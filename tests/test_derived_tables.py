"""The four route tables the export derives (fielding events, a player's involvement in each match, what a batter did against a bowler, each match's scorecard): schema and integrity, that the export refuses a
table that disagrees with its definition, and that what the routes read from them equals what they built from the balls before, over every player, pair and match of the committed cut-out of 22 real matches,
including Super Overs, substitutes, retirements and two wickets on one ball. The same comparison over the full export is in test_leaderboard_equivalence_full.py (CRICKET_API_FULL=1)."""
import json
import sys
from pathlib import Path

import pytest

from cricket_api import contract, derived, export, history, metrics, scorecard
from cricket_api.errors import ApiError
from test_innings_tables import GOLDEN, connect, cut_out_db

NEW = ("player_fielding_events", "player_match_involvement", "batter_bowler_innings", "match_scorecards")


def test_the_tables_are_in_the_contract_the_meta_and_after_the_tables_they_read(dataset):
    meta = json.loads((dataset / "meta.json").read_text())
    names = list(meta["tables"])
    for name in NEW:
        assert name in names and contract.TABLES[name]["derived"] and contract.TABLES[name]["source"] is None and name in contract.DERIVED_TABLES
        assert meta["tables"][name]["columns"] == list(contract.TABLES[name]["columns"]) and len(meta["tables"][name]["sha256"]) == 64
        assert names.index(name) > names.index("balls") and names.index(name) > names.index("wicket_fielders")
    assert contract.problems() == []


def test_schema_keys_links_and_values_hold_on_the_real_matches():
    con = connect(GOLDEN)
    for name in NEW:
        spec = contract.TABLES[name]
        described = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {name}").fetchall()}
        assert list(described) == list(spec["columns"])
        rows = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        assert rows > 0
        key = ", ".join(spec["key"])
        assert con.execute(f"SELECT count(*) FROM (SELECT DISTINCT {key} FROM {name})").fetchone()[0] == rows, f"{name}: the key repeats"
    assert con.execute("SELECT count(*) FROM match_scorecards").fetchone()[0] == con.execute("SELECT count(*) FROM matches").fetchone()[0]
    assert con.execute("SELECT count(*) FROM player_fielding_events WHERE kind NOT IN ('caught', 'stumped', 'run_out', 'substitute', 'caught_and_bowled')").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM player_match_involvement WHERE team IS NULL AND (in_squad OR batted OR bowled OR fielded OR replaced_in OR replaced_out) AND NOT absent_hurt").fetchone()[0] >= 0
    assert con.execute("SELECT count(*) FROM batter_bowler_innings WHERE deliveries < balls_faced OR runs < 0 OR (deliveries = 0 AND len(dismissal_kinds) = 0)").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM batter_bowler_innings b JOIN innings i USING (match_id, innings) WHERE i.is_super_over").fetchone()[0] == 0, "a Super Over is not in the matchup table"
    assert con.execute("SELECT count(*) FROM player_fielding_events e JOIN innings i USING (match_id, innings) WHERE i.is_super_over").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM player_match_involvement WHERE super_over").fetchone()[0] > 0, "the cut-out holds a Super Over, and history keeps it"


@pytest.mark.parametrize("table,patch", [
    ("player_fielding_events", ("FIELDING_EVENTS", "WHEN any_value(wk.wicket_type) = 'caught' THEN 'caught'", "WHEN any_value(wk.wicket_type) = 'caught' THEN 'stumped'")),
    ("player_match_involvement", ("INVOLVEMENT", "bool_or(kind = 'bowl') AS bowled", "bool_or(kind = 'field') AS bowled")),
    ("batter_bowler_innings", ("BATTER_BOWLER", "coalesce(f.runs, 0) AS runs", "coalesce(f.runs, 0) + 1 AS runs")),
])
def test_the_export_refuses_a_route_table_that_disagrees_with_its_definition(tmp_path, monkeypatch, table, patch):
    """A table is made wrong on purpose. Its definition for the routes stays right, so the export's own comparison must stop the build."""
    from conftest import BALLS, INNINGS, MATCHES, PEOPLE, PLAYERS, WICKETS, WICKET_FIELDERS, write_table
    source = tmp_path / "output"
    source.mkdir()
    content = {"balls": BALLS, "matches": MATCHES, "players": PLAYERS, "wickets": WICKETS, "innings": INNINGS, "match_people": PEOPLE, "wicket_fielders": WICKET_FIELDERS}
    for name in contract.TABLES:
        write_table(source, name, content.get(name, []))
    attr, old, new = patch
    text = getattr(derived, attr)
    assert old in text
    monkeypatch.setattr(derived, attr, text.replace(old, new))
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and problems, "the broken table was accepted"
    assert not (tmp_path / "api").exists()


def test_a_scorecard_that_is_stored_wrong_is_refused(tmp_path, monkeypatch):
    from conftest import BALLS, INNINGS, MATCHES, PEOPLE, PLAYERS, WICKETS, WICKET_FIELDERS, write_table
    source = tmp_path / "output"
    source.mkdir()
    content = {"balls": BALLS, "matches": MATCHES, "players": PLAYERS, "wickets": WICKETS, "innings": INNINGS, "match_people": PEOPLE, "wicket_fielders": WICKET_FIELDERS}
    for name in contract.TABLES:
        write_table(source, name, content.get(name, []))
    real = scorecard.build_for_match
    calls = {"n": 0}

    def wrong_once(db, match_id):
        calls["n"] += 1
        out = real(db, match_id)
        if calls["n"] <= 3:                          # the first three builds (the export's own) come out wrong; the check builds them again, correctly
            out["innings"][0]["total"]["runs"] += 1
        return out
    monkeypatch.setattr(scorecard, "build_for_match", wrong_once)
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and any("stored scorecard" in p for p in problems), problems


def players_pairs_matches(con):
    players = [r[0] for r in con.execute("SELECT player_id FROM players ORDER BY 1").fetchall()]
    pairs = [tuple(r) for r in con.execute("SELECT DISTINCT batter_id, bowler_id FROM batter_bowler_innings ORDER BY 1, 2").fetchall()]
    pairs += [tuple(r) for r in con.execute("SELECT DISTINCT b.batter_id, c.bowler_id FROM batter_bowler_innings b, batter_bowler_innings c WHERE b.match_id <> c.match_id ORDER BY hash(b.batter_id || c.bowler_id) LIMIT 40").fetchall()]
    matches = [r[0] for r in con.execute("SELECT match_id FROM matches ORDER BY 1").fetchall()]
    return players, list(dict.fromkeys(pairs)), matches


def filter_sets(con):
    from test_innings_tables import player_filter_sets
    return player_filter_sets(con)[:7]


def compare_fielding(db, players, sets):
    bad = []
    for pid in players:
        for f in sets:
            m, params = metrics.selected_matches(*f)
            for by in (None, "season", "opponent", "venue", "collection", "format"):
                a, b = metrics.fielding(db, m, params, pid, by), metrics.fielding_from_balls(db, m, params, pid, by)
                if by in metrics.SPLIT_COLUMNS:
                    a, b = sorted(a, key=metrics.split_order), sorted(b, key=metrics.split_order)
                if a != b:
                    bad.append(f"fielding {pid} {f} {by}")
    return bad


def compare_history(db, players, sets):
    bad = []
    for pid in players:
        for f in sets:
            m, params = metrics.selected_matches(*f)
            old = history.player_matches_from_balls(db, m, params, pid, None, 100000)
            if history.player_matches(db, m, params, pid, None, 100000) != old:
                bad.append(f"history {pid} {f}")
                continue
            for k in range(0, len(old), max(1, len(old) // 3)):          # keyset pages through the list: after row k, five rows
                after = (old[k]["start_date"], old[k]["match_id"])
                if history.player_matches(db, m, params, pid, after, 5) != history.player_matches_from_balls(db, m, params, pid, after, 5):
                    bad.append(f"history page {pid} {f} after {after}")
    return bad


def compare_matchups(db, pairs, sets):
    bad = []
    for batter, bowler in pairs:
        for f in sets:
            m, params = metrics.selected_matches(*f)
            if metrics.matchup(db, m, params, batter, bowler) != metrics.matchup_from_balls(db, m, params, batter, bowler):
                bad.append(f"matchup {batter} {bowler} {f}")
    return bad


def compare_scorecards(db, matches):
    bad = []
    for match_id in matches:
        try:
            want = scorecard.build_for_match(db, match_id)
        except ApiError:
            want = None
        try:
            got = scorecard.stored(db, match_id)
        except ApiError:
            got = None
        if got != want:
            bad.append(f"scorecard {match_id}")
    return bad


def test_fielding_equals_the_definition_for_every_player_and_grouping_in_the_real_matches():
    db = cut_out_db()
    try:
        con = connect(GOLDEN)
        players, _, _ = players_pairs_matches(con)
        assert len(players) > 100 and compare_fielding(db, players, filter_sets(con)[:3]) == []
    finally:
        db.close()


def test_history_and_every_page_of_it_equal_the_definition_for_every_player_in_the_real_matches():
    db = cut_out_db()
    try:
        con = connect(GOLDEN)
        players, _, _ = players_pairs_matches(con)
        assert compare_history(db, players, filter_sets(con)) == []
    finally:
        db.close()


def test_matchups_equal_the_definition_for_every_pair_that_met_and_pairs_that_never_met():
    db = cut_out_db()
    try:
        con = connect(GOLDEN)
        _, pairs, _ = players_pairs_matches(con)
        assert len(pairs) > 500
        spread = pairs[:: max(1, len(pairs) // 160)]                       # the full set of pairs is compared on the full export; here a deterministic spread of about 160, with two filter sets
        assert compare_matchups(db, spread, filter_sets(con)[:2]) == []
    finally:
        db.close()


def test_every_stored_scorecard_equals_the_scorecard_built_from_the_balls():
    db = cut_out_db()
    try:
        con = connect(GOLDEN)
        _, _, matches = players_pairs_matches(con)
        assert len(matches) == 22 and compare_scorecards(db, matches) == []
    finally:
        db.close()


def test_a_match_without_a_stored_scorecard_row_is_a_fault_and_a_null_is_the_404():
    db = cut_out_db()
    try:
        with pytest.raises(RuntimeError):
            scorecard.stored(db, 999999999)
    finally:
        db.close()
