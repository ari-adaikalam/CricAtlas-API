"""Write the API's Parquet files from the dataset's CSV files.

    python -m cricket_api.export [--source api/data/csv] [--out api/data/exports/manual]

Reads ball_by_ball_data.csv, matches.csv and players.csv, keeps only the columns in contract.py, sorts the rows so one
match sits together, and writes balls.parquet, matches.parquet and players.parquet plus meta.json. The files are built in
a scratch folder, checked against the CSVs, and moved into place only when every check passes. A failed run leaves the
files from the last good run as they were.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from cricket_api import contract, derived, history, metrics, player_universe, scorecard
from cricket_api.errors import ApiError

ROW_GROUP_SIZE = 100_000
SCORECARD_ROW_GROUP_SIZE = 2048      # one scorecard is a long text and a request reads one match: small row groups let the engine skip the others (measured 51 ms a lookup with one group, 16 ms with 2048-row groups)
SOURCE_NOTE = {
    "source": "Cricsheet", "source_url": "https://cricsheet.org/",
    "licence": "Open Data Commons Attribution License 1.0", "licence_url": "https://opendatacommons.org/licenses/by/1-0/",
    "attribution": "Contains data from Cricsheet (https://cricsheet.org/), made available under the Open Data Commons Attribution License 1.0 (ODC-By). "
                   "You may share and adapt it with attribution.",
}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(path: Path | str) -> str:
    """A path as an SQL string, with any quote doubled. The paths come from the command line, but they are quoted all the same."""
    return "'" + str(path).replace("'", "''") + "'"


def export_connection():
    """Optional build limits affect the exporter, never the live API settings."""
    limits = {}
    if "CRICKET_EXPORT_MEMORY" in os.environ:
        value = os.environ["CRICKET_EXPORT_MEMORY"]
        if re.fullmatch(r"[1-9][0-9]*(?:KB|MB|GB|KiB|MiB|GiB)", value) is None:
            raise ValueError("CRICKET_EXPORT_MEMORY must be a positive memory size")
        limits["memory_limit"] = value
    if "CRICKET_EXPORT_THREADS" in os.environ:
        value = int(os.environ["CRICKET_EXPORT_THREADS"])
        if not 1 <= value <= 64:
            raise ValueError("CRICKET_EXPORT_THREADS must be between 1 and 64")
        limits["threads"] = value
    if os.environ.get("CRICKET_EXPORT_TEMP"):
        path = Path(os.environ["CRICKET_EXPORT_TEMP"])
        path.mkdir(parents=True, exist_ok=True)
        limits.update(temp_directory=str(path), max_temp_directory_size="2GiB")
    return duckdb.connect(config=limits)


def default_data_dir() -> Path:
    """The API's own data folder (api/data, from the repository root), where the API refresh keeps its CSV files and exports."""
    return Path(os.environ.get("CRICKET_API_DATA_ROOT") or "api/data")


def csv_source(path: Path) -> str:
    """Every column is read as text, then cast on purpose. A guessed type would turn a season such as 2001/02 into a date."""
    return f"read_csv({lit(path)}, header = true, all_varchar = true)"


def select_list(columns: dict[str, str]) -> str:
    return ", ".join(f"CAST({quote(name)} AS {kind}) AS {quote(name)}" for name, kind in columns.items())


def relations(source: Path) -> dict:
    """Every CSV the player universe reads, as SQL, with every column read as text."""
    names = {"players": "players.csv", "balls": "ball_by_ball_data.csv", "match_people": "match_people.csv", "wicket_fielders": "wicket_fielders.csv", "wickets": "wickets.csv",
             "innings_absent_hurt": "innings_absent_hurt.csv", "match_replacements": "match_replacements.csv"}
    return {key: csv_source(source / file) for key, file in names.items()}


def views_over(folder: Path, names) -> duckdb.DuckDBPyConnection:
    """A private connection with one view per Parquet file in `folder`, named as the API's own are, so the SQL of metrics.py runs here exactly as it does in the server."""
    con = export_connection()
    for name in names:
        con.execute(f"CREATE VIEW {quote(name)} AS SELECT * FROM read_parquet({lit(folder / (name + '.parquet'))})")
    return con


def write_innings_table(name: str, target: Path) -> None:
    """player_batting_innings and player_bowling_spells, computed with the career rules (metrics.py) from the Parquet files already written beside the target, over every match."""
    every_match = metrics.selected_matches(None, None, None, None, None, None)[0]
    if name == "player_batting_innings":
        rows = f"WITH m AS ({every_match}), {metrics.batting_innings(False, 'opponent')} {metrics.BATTING_INNINGS_ROWS}"
    else:
        rows = metrics.bowling_all_from_balls(every_match) + " " + metrics.BOWLING_SPELLS_ROWS
    order = ", ".join(quote(c) for c in contract.TABLES[name]["order"])
    con = base_views(target)
    try:
        con.execute(f"COPY (SELECT {select_list(contract.TABLES[name]['columns'])} FROM ({rows}) ORDER BY {order}) TO {lit(target)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {ROW_GROUP_SIZE})")
    finally:
        con.close()


class QueryAdapter:
    """The one method of the server's Database that the route code uses (`query`), over a private connection, so that metrics.py, history.py and scorecard.py run here as they run in the server."""
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con

    def query(self, sql: str, params=()) -> list[dict]:
        cursor = self.con.cursor()
        try:
            result = cursor.execute(sql, list(params))
            names = [d[0] for d in result.description]
            return [dict(zip(names, row)) for row in result.fetchall()]
        finally:
            cursor.close()


def base_views(target: Path) -> duckdb.DuckDBPyConnection:
    """Views over the tables already written beside `target` that a derived table is built from (the derived tables themselves are left out)."""
    return views_over(target.parent, sorted(f.stem for f in target.parent.glob("*.parquet") if f.stem not in contract.DERIVED_TABLES))


def write_sql_derived(name: str, target: Path) -> None:
    """player_fielding_events, player_match_involvement and batter_bowler_innings: one SELECT each over the ball, wicket, squad and match tables (derived.py)."""
    sql = {"player_fielding_events": derived.FIELDING_EVENTS, "player_match_involvement": derived.INVOLVEMENT, "batter_bowler_innings": derived.BATTER_BOWLER}[name]
    order = ", ".join(quote(c) for c in contract.TABLES[name]["order"])
    con = base_views(target)
    try:
        con.execute(f"COPY (SELECT {select_list(contract.TABLES[name]['columns'])} FROM ({sql}) ORDER BY {order}) TO {lit(target)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {ROW_GROUP_SIZE})")
    finally:
        con.close()


scorecard_cache = None      # set by the daily refresh to its kept scorecards (cricket_refresh.incremental.State); None everywhere else, which builds every scorecard as before


def write_scorecards(target: Path) -> None:
    """match_scorecards: the data the scorecard route returns, built by scorecard.build_for_match for every match and stored as compact JSON text. A match with no innings stores NULL (the route answers 404
    as the definition does). Takes about 50 ms a match."""
    con = base_views(target)
    try:
        db = QueryAdapter(con)
        cache = scorecard_cache

        def fresh(match_id):
            try:
                return json.dumps(scorecard.build_for_match(db, match_id), separators=(",", ":"), ensure_ascii=False)
            except ApiError as error:
                if error.status != 404:
                    raise
                return None

        rows = []
        for (match_id,) in con.execute("SELECT match_id FROM matches ORDER BY match_id").fetchall():
            kept = cache.get_scorecard(match_id) if cache is not None else None
            if cache is None or kept is cache.MISSING:
                text = fresh(match_id)
                if cache is not None:
                    cache.put_scorecard(match_id, text)
            else:
                text = kept
            rows.append((match_id, text))
        if cache is not None:
            cache.flush()
            cache.audit_scorecards(fresh)
        con.execute("CREATE TABLE built (match_id INTEGER, scorecard VARCHAR)")
        con.executemany("INSERT INTO built VALUES (?, ?)", rows)
        con.execute(f"COPY (SELECT {select_list(contract.TABLES['match_scorecards']['columns'])} FROM built ORDER BY match_id) TO {lit(target)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {SCORECARD_ROW_GROUP_SIZE})")
    finally:
        con.close()


def write_derived(con: duckdb.DuckDBPyConnection, name: str, source: Path, target: Path) -> None:
    if name in contract.INNINGS_TABLES:
        return write_innings_table(name, target)
    if name in contract.DERIVED_SQL_TABLES:
        return write_sql_derived(name, target)
    if name == "match_scorecards":
        return write_scorecards(target)
    sources = player_universe.sources_sql(relations(source))
    sql = (player_universe.PLAYERS_SQL if name == "players" else player_universe.NAMES_SQL).format(sources=sources)
    order = ", ".join(quote(c) for c in contract.TABLES[name]["order"])
    con.execute(f"COPY (SELECT {select_list(contract.TABLES[name]['columns'])} FROM ({sql}) ORDER BY {order}) TO {lit(target)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {ROW_GROUP_SIZE})")


def write_table(con: duckdb.DuckDBPyConnection, name: str, source: Path, target: Path, tables: dict) -> None:
    spec = tables[name]
    if spec.get("derived"):
        return write_derived(con, name, source, target)
    order = ", ".join(quote(c) for c in spec["order"])
    con.execute(
        f"COPY (SELECT {select_list(spec['columns'])} FROM {csv_source(source / spec['source'])} ORDER BY {order}) "
        f"TO {lit(target)} (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
    )


def count(con: duckdb.DuckDBPyConnection, relation: str) -> int:
    return con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]


def check_table(con: duckdb.DuckDBPyConnection, name: str, source: Path, parquet: Path, tables: dict) -> list[str]:
    """Compare one Parquet file with the CSV it came from."""
    spec, found = tables[name], []
    described = con.execute(f"DESCRIBE SELECT * FROM read_parquet({lit(parquet)})").fetchall()
    names = [row[0] for row in described]
    if names != list(spec["columns"]):
        found.append(f"{name}: the columns are not the allowed columns, in order ({names})")
    for column in names:
        if contract.banned(column):
            found.append(f"{name}.{column} is a banned name")
    if spec.get("derived"):             # built from several CSV files, so it is checked on its own terms in check_players
        key = ", ".join(quote(c) for c in spec["key"])
        rows = count(con, f"read_parquet({lit(parquet)})")
        if count(con, f"(SELECT DISTINCT {key} FROM read_parquet({lit(parquet)}))") != rows:
            found.append(f"{name}: rows repeat the key {spec['key']}")
        return found
    csv_rows, parquet_rows = count(con, csv_source(source / spec["source"])), count(con, f"read_parquet({lit(parquet)})")
    if csv_rows != parquet_rows:
        found.append(f"{name}: {parquet_rows:,} rows in the Parquet file, {csv_rows:,} in the CSV")
    key = ", ".join(quote(c) for c in spec["key"])
    distinct = count(con, f"(SELECT DISTINCT {key} FROM read_parquet({lit(parquet)}))")
    if distinct != parquet_rows:
        found.append(f"{name}: {parquet_rows - distinct:,} rows repeat the key {spec['key']}")
    # A value that turned empty or changed on the way would show up as a different count of filled cells.
    csv_filled = con.execute("SELECT " + ", ".join(f"count({quote(c)})" for c in spec["columns"]) + f" FROM {csv_source(source / spec['source'])}").fetchone()
    parquet_filled = con.execute("SELECT " + ", ".join(f"count({quote(c)})" for c in spec["columns"]) + f" FROM read_parquet({lit(parquet)})").fetchone()
    for column, before, after in zip(spec["columns"], csv_filled, parquet_filled):
        if before != after:
            found.append(f"{name}.{column}: {after:,} filled cells in the Parquet file, {before:,} in the CSV")
    return found


def check_links(con: duckdb.DuckDBPyConnection, folder: Path) -> list[str]:
    found = []
    for table, column, parent, parent_column in contract.LINKS:
        missing = count(con, (
            f"(SELECT DISTINCT {quote(column)} AS v FROM read_parquet({lit(folder / (table + '.parquet'))}) WHERE {quote(column)} IS NOT NULL) c "
            f"ANTI JOIN (SELECT DISTINCT {quote(parent_column)} AS v FROM read_parquet({lit(folder / (parent + '.parquet'))})) p ON c.v = p.v"))
        if missing:
            found.append(f"{missing:,} values of {table}.{column} are not in {parent}.{parent_column}")
    matches_without_balls = count(con, (
        f"(SELECT match_id FROM read_parquet({lit(folder / 'matches.parquet')})) m "
        f"ANTI JOIN (SELECT DISTINCT match_id FROM read_parquet({lit(folder / 'balls.parquet')})) b USING (match_id)"))
    if matches_without_balls:
        found.append(f"{matches_without_balls:,} matches have no balls")
    return found


KNOWN_OUTCOMES = ("tie", "draw", "no result")


def check_results(con: duckdb.DuckDBPyConnection, source: Path) -> list[str]:
    """Every match has a winner or one of the outcomes the team records know how to count. A match with neither would be a result the rules do not cover, so the export stops."""
    rows = con.execute(f"SELECT coalesce(outcome, '(empty)') AS outcome, count(*) FROM {csv_source(source / 'matches.csv')} WHERE (winner IS NULL OR winner = '') "
                       f"AND (outcome IS NULL OR outcome NOT IN ({', '.join(repr(o) for o in KNOWN_OUTCOMES)})) GROUP BY 1").fetchall()
    return [f"matches: {n:,} matches have no winner and the outcome {outcome!r}, which the team records cannot count (known outcomes: {', '.join(KNOWN_OUTCOMES)})" for outcome, n in rows]


def check_players(con: duckdb.DuckDBPyConnection, source: Path, folder: Path) -> tuple[list[str], dict]:
    """The player table against the CSV files it came from. Returns the problems and a short report that goes into meta.json."""
    found = []
    players, names = f"read_parquet({lit(folder / 'players.parquet')})", f"read_parquet({lit(folder / 'player_names.parquet')})"
    sources = player_universe.sources_sql(relations(source))
    original = count(con, f"(SELECT DISTINCT player_id FROM {csv_source(source / 'players.csv')})")
    missing = count(con, f"(SELECT DISTINCT player_id AS id FROM {csv_source(source / 'players.csv')}) a ANTI JOIN {players} p ON p.player_id = a.id")
    if missing:
        found.append(f"players: {missing:,} players of players.csv are not in the player table")
    no_name = count(con, f"{players} p ANTI JOIN {names} n ON n.player_id = p.player_id AND n.name = p.name")
    if no_name:
        found.append(f"players: {no_name:,} chosen names are not among the known spellings")
    no_row = count(con, f"(SELECT DISTINCT player_id FROM {names}) n ANTI JOIN {players} p USING (player_id)")
    if no_row:
        found.append(f"players: {no_row:,} ids with a spelling have no player row")
    officials = player_universe.OFFICIALS_ONLY_SQL.format(sources=sources, match_people=csv_source(source / "match_people.csv"))
    leaked = count(con, f"({officials}) o SEMI JOIN {players} p ON p.player_id = o.id")
    if leaked:
        found.append(f"players: {leaked:,} people known only as officials are in the player table")
    for (value,) in con.execute(f"SELECT DISTINCT source FROM read_parquet({lit(folder / 'player_external_ids.parquet')})").fetchall():
        if contract.banned(value or ""):
            found.append(f"player_external_ids: the source {value!r} is a banned name, so its ids must not be served")
    total = count(con, players)
    spellings = con.execute(f"SELECT player_id, list(name) FROM {names} GROUP BY player_id HAVING count(*) > 1").fetchall()
    report = {"players": total, "from_players_csv": original, "added": total - original,
              "ids_with_more_than_one_spelling": len(spellings), "ids_with_conflicting_spellings": sum(1 for _, n in spellings if player_universe.conflicting(n))}
    return found, report


def check_innings_tables(folder: Path, tables: dict) -> list[str]:
    """The two per-innings tables against the definition. For every match, for each collection and each format (the filters the leaderboards take), every player's totals read from the table
    must equal the totals built from the ball rows, in every column. A difference stops the export, so a leaderboard can never be served from a table that disagrees with the career rules."""
    con = views_over(folder, tables)
    found = []
    try:
        collections = [r[0] for r in con.execute("SELECT DISTINCT collection FROM matches WHERE collection IS NOT NULL ORDER BY 1").fetchall()]
        formats = [r[0] for r in con.execute("SELECT DISTINCT format FROM matches WHERE format IS NOT NULL ORDER BY 1").fetchall()]
        for label, filters in [("all matches", (None,) * 6)] + [(f"collection {c}", (c, None, None, None, None, None)) for c in collections] + [(f"format {f}", (None, f, None, None, None, None)) for f in formats]:
            matches_sql, params = metrics.selected_matches(*filters)
            for kind, rebuilt, stored in (("batting", metrics.batting_all_from_balls, metrics.batting_all), ("bowling", metrics.bowling_all_from_balls, metrics.bowling_all)):
                a = con.execute(rebuilt(matches_sql) + " SELECT * FROM totals ORDER BY pid", params).fetchall()
                b = con.execute(stored(matches_sql) + " SELECT * FROM totals ORDER BY pid", params).fetchall()
                if a != b:
                    found.append(f"{kind} totals read from the per-innings table differ from the totals built from the balls ({label}: {len(b):,} players against {len(a):,})")
        # the per-player routes (career, seasons, splits) read the same tables and add the opponent from the innings and matches tables: every row, with its opponent, must equal the one built from the balls
        every = metrics.selected_matches(None, None, None, None, None, None)[0]
        for kind, relation, built, stored in (("batting", "played", metrics.batting_innings(False, "opponent"), metrics.batting_played_table("opponent")),
                                              ("bowling", "spells", metrics.bowling_spells(False), metrics.bowling_spells_table("opponent"))):
            a, b = (f"(WITH m AS ({every}), {text} SELECT * FROM {relation})" for text in (built, stored))
            only_balls, only_table = (con.execute(f"SELECT count(*) FROM ({x} EXCEPT ALL {y})").fetchone()[0] for x, y in ((a, b), (b, a)))
            if only_balls or only_table:
                found.append(f"{kind} rows with the opponent differ between the per-innings table and the balls: {only_balls:,} only from the balls, {only_table:,} only from the table")
    finally:
        con.close()
    return found


SAMPLE_PLAYERS_BUSIEST, SAMPLE_PLAYERS_SPREAD, SAMPLE_PAIRS, SAMPLE_MATCHES = 6, 14, 18, 40
GROUPINGS = (None, "season", "opponent", "venue", "collection", "format")


def comparable(rows, by):
    """A split comes back in the engine's order and the route sorts it with split_order, which is total, so two answers are compared sorted."""
    return sorted(rows, key=metrics.split_order) if by in metrics.SPLIT_COLUMNS else rows


def check_route_tables(folder: Path, tables: dict) -> list[str]:
    """What the routes read from fielding, history, matchup and scorecard tables against what they built before from the balls, for a deterministic sample: the busiest players, a spread of others by hash, the
    busiest batter and bowler pairs and a spread of others, and a spread of matches. The tables themselves are written by the same rules (derived.py), so this is the check that the routes' reading of them,
    with the filters, the groupings, the pagination and the opponent, gives the answers the definitions give. The full comparison over every player is the test on the full export."""
    con = views_over(folder, tables)
    db = QueryAdapter(con)
    found = []
    try:
        players = [r[0] for r in con.execute("SELECT player_id FROM (SELECT player_id, count(*) n FROM player_match_involvement GROUP BY 1) ORDER BY n DESC, player_id LIMIT ?", [SAMPLE_PLAYERS_BUSIEST]).fetchall()]
        players += [r[0] for r in con.execute("SELECT player_id FROM (SELECT DISTINCT player_id FROM player_match_involvement) ORDER BY hash(player_id), player_id LIMIT ?", [SAMPLE_PLAYERS_SPREAD]).fetchall()]
        every, no_params = metrics.selected_matches(None, None, None, None, None, None)[0], []
        for pid in dict.fromkeys(players):
            for by in GROUPINGS:
                if comparable(metrics.fielding(db, every, no_params, pid, by), by) != comparable(metrics.fielding_from_balls(db, every, no_params, pid, by), by):
                    found.append(f"fielding of {pid} grouped by {by} differs between player_fielding_events and the balls")
            new, old = history.player_matches(db, every, no_params, pid, None, 100000), history.player_matches_from_balls(db, every, no_params, pid, None, 100000)
            if new != old:
                found.append(f"the match history of {pid} differs between player_match_involvement and the records")
            elif len(old) > 3:
                key = (old[len(old) // 2]["start_date"], old[len(old) // 2]["match_id"])
                if history.player_matches(db, every, no_params, pid, key, 5) != history.player_matches_from_balls(db, every, no_params, pid, key, 5):
                    found.append(f"a page of the match history of {pid} differs between player_match_involvement and the records")
        pairs = [tuple(r) for r in con.execute("SELECT batter_id, bowler_id FROM (SELECT batter_id, bowler_id, sum(deliveries) n FROM batter_bowler_innings GROUP BY 1, 2) ORDER BY n DESC, batter_id, bowler_id LIMIT ?", [SAMPLE_PAIRS // 2]).fetchall()]
        pairs += [tuple(r) for r in con.execute("SELECT batter_id, bowler_id FROM (SELECT DISTINCT batter_id, bowler_id FROM batter_bowler_innings) ORDER BY hash(batter_id || bowler_id), batter_id LIMIT ?", [SAMPLE_PAIRS - SAMPLE_PAIRS // 2]).fetchall()]
        for batter, bowler in dict.fromkeys(pairs):
            if metrics.matchup(db, every, no_params, batter, bowler) != metrics.matchup_from_balls(db, every, no_params, batter, bowler):
                found.append(f"the matchup of {batter} against {bowler} differs between batter_bowler_innings and the balls")
        for (match_id,) in con.execute("SELECT match_id FROM matches ORDER BY hash(match_id), match_id LIMIT ?", [SAMPLE_MATCHES]).fetchall():
            try:
                want = scorecard.build_for_match(db, match_id)
            except ApiError:
                want = None
            row = db.query("SELECT scorecard FROM match_scorecards WHERE match_id = ?", [match_id])
            if len(row) != 1 or (None if row[0]["scorecard"] is None else json.loads(row[0]["scorecard"])) != want:
                found.append(f"the stored scorecard of match {match_id} differs from the scorecard built from the balls")
        stored, wanted = con.execute("SELECT count(*) FROM match_scorecards").fetchone()[0], con.execute("SELECT count(*) FROM matches").fetchone()[0]
        if stored != wanted:
            found.append(f"match_scorecards has {stored:,} rows for {wanted:,} matches")
    finally:
        con.close()
    return found


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 23), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_date(source: Path) -> str | None:
    """The Cricsheet download date, when the dataset's manifest sits next to the CSV files."""
    manifest = source / "manifest.json"
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text(encoding="utf-8")).get("snapshot_date")


def write_meta(folder: Path, source: Path, specs: dict, features: dict, players: dict) -> dict:
    tables = {}
    for name, spec in specs.items():
        path = folder / f"{name}.parquet"
        with export_connection() as con:
            tables[name] = {"file": path.name, "rows": count(con, f"read_parquet({lit(path)})"), "bytes": path.stat().st_size,
                            "sha256": sha256(path), "columns": list(spec["columns"])}
    # One short id for the whole data set. It changes only when a file does, so it can key a cache.
    version = hashlib.sha256("".join(t["sha256"] for t in tables.values()).encode()).hexdigest()[:12]
    meta = {"data_version": version, "built_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "snapshot_date": snapshot_date(source), **SOURCE_NOTE, "features": {"player_profiles": features["player_profiles"]}, "player_universe": players, "tables": tables}
    (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def header_of(path: Path) -> list[str]:
    with open(path, encoding="utf-8", newline="") as handle:
        return next(csv.reader(handle))


def build(source: Path, out: Path) -> tuple[dict | None, list[str]]:
    """Returns the meta and an empty list of problems when the files are good and in place, or None and the problems."""
    faults = contract.problems()
    if faults:
        return None, faults
    for name, spec in contract.csv_tables().items():
        if not (source / spec["source"]).exists():
            return None, [f"{source / spec['source']} is missing"]
    tables, features = contract.effective_tables(header_of(source / contract.TABLES["balls"]["source"]))
    if features["partial_profiles"]:
        return None, ["the ball file has some of the player profile columns but not all of them"]
    scratch = out.parent / f".{out.name}.building"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    con = export_connection()
    try:
        for name in tables:
            write_table(con, name, source, scratch / f"{name}.parquet", tables)
        found = []
        for name in tables:
            found += check_table(con, name, source, scratch / f"{name}.parquet", tables)
        found += check_links(con, scratch)
        more, players = check_players(con, source, scratch)
        found += more + check_results(con, source)
        found += check_innings_tables(scratch, tables)
        found += check_route_tables(scratch, tables)
        if found:
            return None, found
        meta = write_meta(scratch, source, tables, features, players)
        shutil.rmtree(out, ignore_errors=True)
        scratch.rename(out)
        return meta, []
    finally:
        con.close()
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the API's Parquet files from the dataset's CSV files.")
    parser.add_argument("--source", type=Path, default=default_data_dir() / "csv", help="folder with the CSV files the API refresh built (default: %(default)s)")
    parser.add_argument("--out", type=Path, default=default_data_dir() / "exports" / "manual", help="folder for the Parquet files (default: %(default)s; the refresh writes api/data/exports/<data version> instead)")
    args = parser.parse_args(argv)
    meta, problems = build(args.source, args.out)
    if problems:
        print("The API data was not written. Nothing in", args.out, "changed.")
        for line in problems:
            print("PROBLEM ", line)
        return 1
    for name, table in meta["tables"].items():
        print(f"{table['file']:<18} {table['rows']:>10,} rows  {table['bytes'] / 1e6:>8.1f} MB")
    u = meta["player_universe"]
    print(f"{u['players']:,} players ({u['added']:,} beyond players.csv), {u['ids_with_conflicting_spellings']:,} with conflicting spellings")
    print(f"data version {meta['data_version']}, written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
