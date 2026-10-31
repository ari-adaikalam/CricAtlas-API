"""The per-innings tables (player_batting_innings, player_bowling_spells) the leaderboards read.

Routine checks, which run with the other tests on the small fixtures: the tables' schema and integrity, that the export refuses to write data whose table disagrees with the career rules, that the leaderboards
no longer read the ball table, and that the leaderboard totals read from the tables equal the totals built from the balls over the committed cut-out of 22 real matches and every filter that exists in it.
The same comparison over the full export, with far more filters, is in test_leaderboard_equivalence_full.py and runs only on request (CRICKET_API_FULL=1)."""
import itertools
import json
import shutil
from pathlib import Path

import duckdb
import pytest

from cricket_api import contract, export, leaderboards, metrics

GOLDEN = Path(__file__).parent / "golden" / "api"


def connect(folder: Path) -> duckdb.DuckDBPyConnection:
    meta = json.loads((folder / "meta.json").read_text())
    return export.views_over(folder, meta["tables"])


def test_the_tables_are_in_the_contract_in_the_meta_and_written_after_the_tables_they_read(dataset):
    meta = json.loads((dataset / "meta.json").read_text())
    names = list(meta["tables"])
    for name in contract.INNINGS_TABLES:
        assert name in names and contract.TABLES[name]["derived"] and contract.TABLES[name]["source"] is None
        assert meta["tables"][name]["columns"] == list(contract.TABLES[name]["columns"])
        assert names.index(name) > names.index("balls") and names.index(name) > names.index("match_people")
        assert (dataset / meta["tables"][name]["file"]).exists() and len(meta["tables"][name]["sha256"]) == 64


@pytest.mark.parametrize("folder", [GOLDEN], ids=["cut-out"])
def test_schema_types_keys_and_links_hold_on_the_real_matches(folder):
    con = connect(folder)
    for name in contract.INNINGS_TABLES:
        spec = contract.TABLES[name]
        described = con.execute(f"DESCRIBE SELECT * FROM {name}").fetchall()
        assert [r[0] for r in described] == list(spec["columns"])
        assert [r[1] for r in described] == list(spec["columns"].values())
        key = ", ".join(spec["key"])
        rows = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        assert rows > 0 and con.execute(f"SELECT count(*) FROM (SELECT DISTINCT {key} FROM {name})").fetchone()[0] == rows, "the key repeats"
        assert con.execute(f"SELECT count(*) FROM {name} WHERE player_id IS NULL OR match_id IS NULL OR innings IS NULL").fetchone()[0] == 0
        assert con.execute(f"SELECT count(*) FROM {name} t ANTI JOIN players p USING (player_id)").fetchone()[0] == 0, "a player with no player row"
        assert con.execute(f"SELECT count(*) FROM {name} t ANTI JOIN matches m USING (match_id)").fetchone()[0] == 0
        assert con.execute(f"SELECT count(*) FROM {name} t ANTI JOIN innings i USING (match_id, innings)").fetchone()[0] == 0
        assert con.execute(f"SELECT count(*) FROM {name} t JOIN innings i USING (match_id, innings) WHERE i.is_super_over").fetchone()[0] == 0, "a Super Over is not part of a career"
    assert con.execute("SELECT count(*) FROM player_batting_innings WHERE runs < 0 OR balls < 0 OR fours < 0 OR sixes < 0").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM player_bowling_spells WHERE balls < 0 OR runs < 0 OR wickets < 0 OR maidens < 0 OR wickets > 10").fetchone()[0] == 0


def test_the_row_counts_are_the_rows_the_career_rules_build(dataset):
    con = connect(dataset)
    every = metrics.selected_matches(None, None, None, None, None, None)[0]
    assert con.execute(metrics.batting_all_from_balls(every) + " SELECT count(*) FROM played").fetchone()[0] == con.execute("SELECT count(*) FROM player_batting_innings").fetchone()[0]
    assert con.execute(metrics.bowling_all_from_balls(every) + " SELECT count(*) FROM spells").fetchone()[0] == con.execute("SELECT count(*) FROM player_bowling_spells").fetchone()[0]


@pytest.mark.parametrize("table,rows", [("player_batting_innings", "BATTING_INNINGS_ROWS"), ("player_bowling_spells", "BOWLING_SPELLS_ROWS")])
def test_the_export_refuses_to_write_a_table_that_disagrees_with_the_career_rules(tmp_path, monkeypatch, table, rows):
    """The table is made wrong on purpose (one extra run, or one extra wicket). The self-check must stop the export and leave nothing in the output folder."""
    from conftest import BALLS, MATCHES, PEOPLE, PLAYERS, INNINGS, WICKETS, WICKET_FIELDERS, write_table
    source = tmp_path / "output"
    source.mkdir()
    content = {"balls": BALLS, "matches": MATCHES, "players": PLAYERS, "wickets": WICKETS, "innings": INNINGS, "match_people": PEOPLE, "wicket_fielders": WICKET_FIELDERS}
    for name in contract.TABLES:
        write_table(source, name, content.get(name, []))
    broken = getattr(metrics, rows).replace(" runs,", " runs + 1 AS runs,") if table == "player_batting_innings" else getattr(metrics, rows).replace(" wickets,", " wickets + 1 AS wickets,")
    assert broken != getattr(metrics, rows)
    monkeypatch.setattr(metrics, rows, broken)
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and any("differ from the totals built from the balls" in p for p in problems), problems
    assert not (tmp_path / "api").exists()


def test_the_leaderboards_no_longer_read_the_ball_table():
    every = metrics.selected_matches(None, None, None, None, None, None)[0]
    for relation, table in ((metrics.batting_all(every), "player_batting_innings"), (metrics.bowling_all(every), "player_bowling_spells")):
        assert table in relation and " balls b" not in relation and "FROM balls" not in relation and "wickets w" not in relation
    for relation in (metrics.batting_all_from_balls(every), metrics.bowling_all_from_balls(every)):
        assert "FROM balls" in relation, "the definition still reads the balls"


def grid(con):
    """Every filter value present in the data, one at a time, and a few combinations and date ranges."""
    colls = [r[0] for r in con.execute("SELECT DISTINCT collection FROM matches WHERE collection IS NOT NULL ORDER BY 1").fetchall()]
    fmts = [r[0] for r in con.execute("SELECT DISTINCT format FROM matches WHERE format IS NOT NULL ORDER BY 1").fetchall()]
    gens = [r[0] for r in con.execute("SELECT DISTINCT gender FROM matches WHERE gender IS NOT NULL ORDER BY 1").fetchall()]
    seasons = [r[0] for r in con.execute("SELECT DISTINCT season FROM matches WHERE season IS NOT NULL ORDER BY 1").fetchall()]
    dates = [r[0] for r in con.execute("SELECT DISTINCT start_date FROM matches ORDER BY 1").fetchall()]
    combos = [(None,) * 6] + [(c, None, None, None, None, None) for c in colls] + [(None, f, None, None, None, None) for f in fmts] + [(None, None, g, None, None, None) for g in gens]
    combos += [(None, None, None, s, None, None) for s in seasons] + [(c, f, g, None, None, None) for c, f, g in itertools.product(colls[:3], fmts[:2], gens[:2])]
    combos += [(None, None, None, None, a, b) for a, b in zip(dates[::3], dates[2::3])] + [(None, None, None, None, dates[0], None), (None, None, None, None, None, dates[-1]), (None, None, None, None, dates[0], dates[0])]
    return list(dict.fromkeys(combos))


KINDS = (("batting", leaderboards.BATTING, metrics.batting_all_from_balls, metrics.batting_all, "balls_faced", "dismissals"),
         ("bowling", leaderboards.BOWLING, metrics.bowling_all_from_balls, metrics.bowling_all, "balls", "wickets"))


def compare_totals(con, combos) -> list[str]:
    bad = []
    for filters in combos:
        m, params = metrics.selected_matches(*filters)
        for kind, _defs, built, stored, _s, _o in KINDS:
            if con.execute(built(m) + " SELECT * FROM totals ORDER BY pid", params).fetchall() != con.execute(stored(m) + " SELECT * FROM totals ORDER BY pid", params).fetchall():
                bad.append(f"totals {kind} {filters}")
    return bad


def compare_rankings(con, combos) -> tuple[int, list[str]]:
    """Every eligible player in order, with rank and all totals, for every metric and three threshold settings (the defaults, a minimum of 1, and none)."""
    bad, checked = [], 0
    for filters in combos:
        m, params = metrics.selected_matches(*filters)
        for kind, defs, built, stored, sample, other in KINDS:
            for metric, chosen in defs.items():
                for balls, second in ((chosen.min_balls, chosen.min_other), (1, 1), (None, None)):
                    sql, extra = leaderboards.ranking_sql(defs, metric, sample, other, balls, second)
                    a = con.execute(built(m) + " " + sql, [*params, *extra, 1_000_000]).fetchall()
                    b = con.execute(stored(m) + " " + sql, [*params, *extra, 1_000_000]).fetchall()
                    checked += 1
                    if a != b:
                        bad.append(f"ranking {kind} {metric} {filters} min {balls},{second}")
    return checked, bad


def test_totals_and_complete_rankings_read_from_the_tables_equal_those_built_from_the_balls_on_the_real_matches():
    con = connect(GOLDEN)
    combos = grid(con)
    assert len(combos) > 20
    assert compare_totals(con, combos) == []
    checked, bad = compare_rankings(con, combos[:: max(1, len(combos) // 12)])
    assert checked > 500 and bad == []


def test_the_tables_are_deterministic_so_a_rebuild_has_the_same_hashes(tmp_path):
    """Writing the committed cut-out's inputs again gives byte-identical table files, so the data version changes only when the data does."""
    first = json.loads((GOLDEN / "meta.json").read_text())["tables"]
    src = tmp_path / "again"
    shutil.copytree(GOLDEN, src)
    con = duckdb.connect()
    for name in contract.INNINGS_TABLES:
        target = src / f"{name}.parquet"
        target.unlink()
        export.write_innings_table(name, target)
        assert export.sha256(target) == first[name]["sha256"], name
    con.close()


# The per-player routes (career, seasons, splits) --------------------------------------------------------------------------

BY = [None, "season", "opponent", "venue", "collection", "format"]


def player_filter_sets(con) -> list:
    colls = [r[0] for r in con.execute("SELECT DISTINCT collection FROM matches WHERE collection IS NOT NULL ORDER BY 1").fetchall()][:4]
    return [(None,) * 6, (None, "T20", None, None, None, None), (None, "TEST", "male", None, None, None), (None, None, "female", None, None, None), *[(c, None, None, None, None, None) for c in colls],
            (None, None, None, None, "2015-01-01", "2019-12-31")]


def compare_players(db, con, players: list, filters: list) -> tuple[int, list[str]]:
    """The table-backed batting() and bowling() against the definitions built from the balls, for every grouping and every filter set, as the rows the routes turn into answers."""
    bad, checked = [], 0
    for pid in players:
        for f in filters:
            m, params = metrics.selected_matches(*f)
            for by in BY:
                for name in ("batting", "bowling"):
                    new = getattr(metrics, name)(db, m, params, pid, by)
                    old = getattr(metrics, name + "_from_balls")(db, m, params, pid, by)
                    if by in metrics.SPLIT_COLUMNS:      # the groups of a split come back in the engine's order and the route sorts them (metrics.split_order, which is total), so compare them sorted
                        new, old = sorted(new, key=metrics.split_order), sorted(old, key=metrics.split_order)
                    checked += 1
                    if new != old:
                        bad.append(f"{name} {pid} {f} by {by}")
    return checked, bad


def cut_out_db():
    from cricket_api.db import Database
    from cricket_api.settings import Settings
    return Database(Settings(data_dir=GOLDEN, cursor_secret="s" * 32))


def test_career_seasons_and_splits_equal_the_definition_for_every_player_in_the_real_matches_and_every_grouping():
    db = cut_out_db()
    try:
        con = connect(GOLDEN)
        # the players with the most rows in either table, a spread of smaller careers and three players with no career at all; three filter sets (none, a format, a date range) over every grouping
        busiest = [r[0] for r in con.execute("SELECT player_id FROM (SELECT player_id, count(*) n FROM (SELECT player_id FROM player_batting_innings UNION ALL SELECT player_id FROM player_bowling_spells) GROUP BY 1) ORDER BY n DESC, 1 LIMIT 8").fetchall()]
        spread = [r[0] for r in con.execute("SELECT player_id FROM (SELECT DISTINCT player_id FROM player_batting_innings UNION SELECT DISTINCT player_id FROM player_bowling_spells) ORDER BY hash(player_id) LIMIT 8").fetchall()]
        idle = [r[0] for r in con.execute("SELECT player_id FROM players WHERE player_id NOT IN (SELECT player_id FROM player_batting_innings UNION SELECT player_id FROM player_bowling_spells) ORDER BY 1 LIMIT 3").fetchall()]
        sets = player_filter_sets(con)
        checked, bad = compare_players(db, con, list(dict.fromkeys(busiest + spread + idle)), [sets[0], sets[1], sets[-1]])
        assert checked > 400 and bad == [], bad[:5]
    finally:
        db.close()


def test_the_export_refuses_a_batting_table_whose_opponent_differs_from_the_side_that_bowled(tmp_path, monkeypatch):
    from conftest import BALLS, INNINGS, MATCHES, PEOPLE, PLAYERS, WICKETS, WICKET_FIELDERS, write_table
    source = tmp_path / "output"
    source.mkdir()
    content = {"balls": BALLS, "matches": MATCHES, "players": PLAYERS, "wickets": WICKETS, "innings": INNINGS, "match_people": PEOPLE, "wicket_fielders": WICKET_FIELDERS}
    for name in contract.TABLES:
        write_table(source, name, content.get(name, []))
    monkeypatch.setattr(metrics, "BATTING_INNINGS_ROWS", metrics.BATTING_INNINGS_ROWS.replace("innings, opponent,", "innings, upper(opponent) AS opponent,"))
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and any("with the opponent differ" in p for p in problems), problems
    assert not (tmp_path / "api").exists()
