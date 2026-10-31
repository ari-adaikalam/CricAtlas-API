"""Full-export equivalence: the leaderboards read from the per-innings tables must equal the current calculation (built from the balls) on the whole dataset, for every supported filter.

Not a routine test. It needs the full export in api/data/exports/current (with the two per-innings tables) and takes several minutes, so it runs only when asked:

    CRICKET_API_FULL=1 python -m pytest api/tests/test_leaderboard_equivalence_full.py

What it compares, with no tolerance (every column of every player, every rank): the totals relation for 250 or so filter combinations (every collection, format, gender and season, random date
ranges, single days, empty selections and random mixes of all six filters), and the complete ranked list (every eligible player) for every metric, with the metric's default thresholds, a minimum
of 1 and no minimum, over 46 of those combinations. Ties, eligibility and the unrounded value are inside the ranking SQL, which both sides share, so a difference can only come from the totals."""
import datetime
import json
import os
import random

import pytest

from cricket_api import metrics
from test_innings_tables import compare_players, compare_rankings, compare_totals, connect, player_filter_sets
from conftest import FULL

pytestmark = pytest.mark.skipif(not (os.environ.get("CRICKET_API_FULL") and FULL.exists()), reason="the full export is compared only on request (CRICKET_API_FULL=1)")


def full_grid(con):
    rnd = random.Random(7)
    colls = list(metrics.COLLECTION_LABELS)
    seasons = [r[0] for r in con.execute("SELECT DISTINCT season FROM matches WHERE season IS NOT NULL ORDER BY 1").fetchall()]
    dates = [r[0] for r in con.execute("SELECT DISTINCT start_date FROM matches ORDER BY 1").fetchall()]
    fmts, gens = [None, "TEST", "ODI", "T20"], [None, "male", "female"]
    combos = [(None,) * 6] + [(c, None, None, None, None, None) for c in colls] + [(None, f, None, None, None, None) for f in fmts[1:]] + [(None, None, g, None, None, None) for g in gens[1:]]
    combos += [(None, None, None, s, None, None) for s in seasons]
    combos += [(None, None, None, None, *sorted(rnd.sample(dates, 2))) for _ in range(40)]
    combos += [(None, None, None, None, rnd.choice(dates), None) for _ in range(8)] + [(None, None, None, None, None, rnd.choice(dates)) for _ in range(8)]
    day = rnd.choice(dates)
    combos += [(None, None, None, None, day, day), (None, None, None, None, datetime.date(1990, 1, 1), datetime.date(1990, 12, 31)), ("tests", "ODI", None, None, None, None)]
    for _ in range(120):
        combos.append((rnd.choice([None] + colls), rnd.choice(fmts), rnd.choice(gens), rnd.choice([None] * 3 + seasons), *(sorted(rnd.sample(dates, 2)) if rnd.random() < 0.3 else (None, None))))
    return list(dict.fromkeys(combos))


def test_the_totals_are_identical_for_every_filter_combination():
    con = connect(FULL)
    assert "player_batting_innings" in json.loads((FULL / "meta.json").read_text())["tables"], "build the full export again: it has no per-innings tables"
    combos = full_grid(con)
    assert len(combos) > 200
    assert compare_totals(con, combos) == []


def test_the_complete_rankings_are_identical_for_every_metric_and_threshold():
    con = connect(FULL)
    combos = full_grid(con)
    chosen = combos[:: max(1, len(combos) // 45)][:45] + [(None,) * 6]
    checked, bad = compare_rankings(con, chosen)
    assert checked >= 2000 and bad == []


def test_the_per_player_rows_equal_the_definition_for_a_stratified_sample_of_players_every_grouping_and_several_filters():
    """Career, seasons and every split of batting and bowling, from the per-innings tables and from the balls, over 20 size bands of three players each, the busiest players, the ladder's player and players with no
    career, with six filter sets. The relations themselves (every row with its opponent) are compared for all players by the export's own check; this compares what the routes build from them."""
    from cricket_api.db import Database
    from cricket_api.settings import Settings
    con = connect(FULL)
    rnd = random.Random(5)
    ranked = [r[0] for r in con.execute("""SELECT player_id FROM (SELECT player_id, sum(b) t FROM (SELECT player_id, sum(balls) b FROM player_batting_innings GROUP BY 1
                                          UNION ALL SELECT player_id, sum(balls) FROM player_bowling_spells GROUP BY 1) GROUP BY 1) ORDER BY t DESC""").fetchall()]
    players = ranked[:3] + ["e087956b"]
    for k in range(20):
        players += rnd.sample(ranked[k * len(ranked) // 20:(k + 1) * len(ranked) // 20], 3)
    players += [r[0] for r in con.execute("SELECT player_id FROM players WHERE player_id NOT IN (SELECT player_id FROM player_batting_innings UNION SELECT player_id FROM player_bowling_spells) LIMIT 3").fetchall()]
    db = Database(Settings(data_dir=FULL, cursor_secret="s" * 32, query_seconds=120))
    try:
        checked, bad = compare_players(db, con, list(dict.fromkeys(players)), player_filter_sets(con))
    finally:
        db.close()
    assert checked > 5000 and bad == [], bad[:5]


def test_fielding_history_matchup_and_scorecard_routes_equal_their_definitions_on_a_much_larger_sample(monkeypatch):
    """The export runs the same check on a small sample. This widens it (about six times more players, pairs and matches) on the full export, with every grouping, filter and cursor page the check covers."""
    from cricket_api import export
    monkeypatch.setattr(export, "SAMPLE_PLAYERS_BUSIEST", 40)
    monkeypatch.setattr(export, "SAMPLE_PLAYERS_SPREAD", 120)
    monkeypatch.setattr(export, "SAMPLE_PAIRS", 120)
    monkeypatch.setattr(export, "SAMPLE_MATCHES", 300)
    tables = json.loads((FULL / "meta.json").read_text())["tables"]
    assert {"player_fielding_events", "player_match_involvement", "batter_bowler_innings", "match_scorecards"} <= set(tables), "build the full export again: it has no fielding, history, matchup or scorecard tables"
    assert export.check_route_tables(FULL, tables) == []
