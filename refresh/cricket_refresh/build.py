"""Write the CSV files the API export reads, from the per-archive row files and the frozen profile snapshot.

Outputs, in api/data/csv/: ball_by_ball_data.csv, matches.csv, players.csv, player_external_ids.csv, wickets.csv, wicket_fielders.csv,
match_people.csv, innings.csv, powerplays.csv, reviews.csv, match_replacements.csv, match_missing.csv, miscounted_overs.csv,
innings_absent_hurt.csv, match_dates.csv and manifest.json (the Cricsheet snapshot date). The columns and their order are those of the
dataset the live data was built from, so the export and its checks need no change. Matches are handled one at a time, so memory stays small.
"""
import hashlib
import json
from pathlib import Path

import pandas as pd

from cricket_refresh import incremental
from cricket_refresh.config import SOURCES, Source, csv_dir, cricsheet_dir, profile_snapshot, work_dir
from cricket_refresh.cricsheet_json import MATCH_FACT_COLUMNS, NOTE_COLUMNS, TABLE_COLUMNS
from cricket_refresh.profiles import (
    MATCH_FILE_ONLY, MATCH_ON_EVERY_ROW, attach_players, external_ids_file, load_snapshot, matches_file, players_file, without_match_file_columns,
)

CHUNK_ROWS = 250_000

FINAL_COLUMNS = [
    "match_id", "season", "start_date", "innings", "delivery_number", "over", "ball_in_over", "scorecard_ball", "is_legal_delivery", "batting_team", "bowling_team",
    "striker", "striker_id", "non_striker", "bowler", "bowler_id",
    "runs_off_bat", "extras", "wides", "noballs", "byes", "legbyes", "penalty",
    "wicket_type", "player_dismissed", "other_wicket_type", "other_player_dismissed", "wicket",
    "striker balls faced", "total striker runs", "player out runs", "player out balls faced",
    "gender", "event", "format", "type",
    "country_striker", "batting style_striker", "bowling style_striker", "playing role_striker", "striker_age",
    "country_bowler", "batting style_bowler", "bowling style_bowler", "playing role_bowler", "bowler_age",
    "fielder_1", "fielder_1_id", "fielder_1_substitute", "non_boundary",
]
WHOLE_NUMBER_COLUMNS = ["innings", "delivery_number", "over", "ball_in_over", "extras", "wides", "noballs", "byes", "legbyes", "penalty", "wicket",
                        "striker balls faced", "total striker runs", "player out runs", "player out balls faced"]
MATCH_WHOLE_NUMBER_COLUMNS = ["match_number", "winner_runs", "winner_wickets", "scheduled_overs", "balls_per_over", "match_type_number", "cricsheet_revision"]

JSON_FILES = {
    "wickets": ("wickets.csv", ["innings", "delivery_number", "wicket_number"]),
    "wicket_fielders": ("wicket_fielders.csv", ["innings", "delivery_number", "wicket_number", "fielder_number"]),
    "people": ("match_people.csv", ["person_number"]),
    "innings": ("innings.csv", ["innings", "target_runs", "penalty_runs_pre", "penalty_runs_post"]),
    "powerplays": ("powerplays.csv", ["innings", "powerplay_number", "start_over", "start_ball_in_over", "start_delivery_number", "end_over", "end_ball_in_over", "end_delivery_number"]),
    "reviews": ("reviews.csv", ["innings", "delivery_number"]),
    "replacements": ("match_replacements.csv", ["innings", "delivery_number", "replacement_number"]),
    "missing": ("match_missing.csv", ["missing_number", "innings"]),
    "miscounted_overs": ("miscounted_overs.csv", ["innings", "over", "balls"]),
    "absent_hurt": ("innings_absent_hurt.csv", ["innings"]),
    "dates": ("match_dates.csv", ["date_number"]),
}
SORT_KEYS = {
    "wickets": ["match_id", "innings", "delivery_number", "wicket_number"],
    "wicket_fielders": ["match_id", "innings", "delivery_number", "wicket_number", "fielder_number"],
    "people": ["match_id", "role", "person_number"], "innings": ["match_id", "innings"], "powerplays": ["match_id", "innings", "powerplay_number"],
    "reviews": ["match_id", "innings", "delivery_number"], "replacements": ["match_id", "innings", "delivery_number", "replacement_number"],
    "missing": ["match_id", "missing_number"], "miscounted_overs": ["match_id", "innings", "over"],
    "absent_hurt": ["match_id", "innings", "player_id", "player"], "dates": ["match_id", "date_number"],
}


def iter_matches(path: Path):
    """Yield one DataFrame per match from a rows file, reading it in chunks. The last match of a chunk is held back until the next one."""
    held = None
    for chunk in pd.read_csv(path, chunksize=CHUNK_ROWS, dtype={"season": str}, low_memory=False):
        if held is not None:
            chunk = pd.concat([held, chunk], ignore_index=True)
        last = chunk["match_id"].iloc[-1]
        held = chunk[chunk["match_id"] == last]
        chunk = chunk[chunk["match_id"] != last]
        for _, match in chunk.groupby("match_id", sort=False):
            yield match
    if held is not None and len(held):
        yield held


def _fresh_outputs(wanted: list[int], players: pd.DataFrame) -> dict:
    """A fresh output for each wanted match, from the per-archive files, in one pass. For the audit of the kept results."""
    found, remaining = {}, set(wanted)
    for source in SOURCES:
        for rows in iter_matches(work_dir() / f"cricsheet_{source.archive}.csv"):
            match_id = int(rows["match_id"].iloc[0])
            if match_id in remaining:
                found[match_id] = match_output(rows, players)
                remaining.discard(match_id)
        if not remaining:
            break
    return found


def whole_if_possible(values: pd.Series) -> pd.Series:
    """Numbers that are all whole are written as 40 and not 40.0. Anything with a fraction is left alone."""
    numbers = pd.to_numeric(values, errors="coerce")
    return numbers.astype("Int64") if (numbers.dropna() % 1 == 0).all() else numbers


def tidy_numbers(rows: pd.DataFrame) -> pd.DataFrame:
    for column in WHOLE_NUMBER_COLUMNS:
        rows[column] = whole_if_possible(rows[column])
    return rows


def read_json_tables(wanted: list[Source]) -> dict[str, pd.DataFrame]:
    tables = {}
    for name, columns in {**TABLE_COLUMNS, "notes": NOTE_COLUMNS}.items():
        parts = []
        for source in wanted:
            path = work_dir() / f"cricsheet_{source.archive}_{name}.csv"
            if path.exists():
                parts.append(pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""]))
        tables[name] = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)
    return tables


def write_json_files(out: Path, tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    rows = {}
    for name, (file_name, whole) in JSON_FILES.items():
        table = tables[name].copy()
        for column in ["match_id", *whole]:
            table[column] = pd.to_numeric(table[column]).astype("Int64")
        if "target_overs" in table:
            table["target_overs"] = pd.to_numeric(table["target_overs"])
        table.sort_values(SORT_KEYS[name]).reset_index(drop=True)[TABLE_COLUMNS[name]].to_csv(out / file_name, index=False)
        rows[file_name] = len(table)
    return rows


MATCH_FILE_COLUMNS = ["match_id", *MATCH_ON_EVERY_ROW, "venue", "match_number", "toss_winner", "toss_decision", "player_of_match", "player_of_match_2", "winner", "winner_runs", "winner_wickets", "outcome"]


def match_output(rows: pd.DataFrame, players: pd.DataFrame) -> tuple[pd.DataFrame, str, int]:
    """One match's row for matches.csv, its lines of ball_by_ball_data.csv (no header) and its row count. A function of the match's own rows and the profile snapshot, which is what lets it be kept."""
    rows = rows.assign(delivery_number=rows.groupby("innings").cumcount() + 1)
    rows = tidy_numbers(attach_players(rows.copy(), players))
    first = rows[MATCH_FILE_COLUMNS].iloc[[0]]
    final = without_match_file_columns(rows)[FINAL_COLUMNS]
    return first, final.to_csv(index=False, header=False), len(rows)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build() -> dict:
    out = csv_dir()
    snapshot = profile_snapshot()
    players = load_snapshot(snapshot)
    state = incremental.current()
    if state is not None:
        state.profile_digest = file_sha256(snapshot)
    main_path = out / "ball_by_ball_data.csv"
    main_path.unlink(missing_ok=True)
    match_rows = []
    totals = {"matches": 0, "rows": 0}
    with open(main_path, "w", encoding="utf-8", newline="") as main:
        main.write(pd.DataFrame(columns=FINAL_COLUMNS).to_csv(index=False))
        for source in SOURCES:
            for rows in iter_matches(work_dir() / f"cricsheet_{source.archive}.csv"):
                match_id = int(rows["match_id"].iloc[0])
                kept = state.get_rows(match_id) if state is not None else None
                if kept is None:
                    kept = match_output(rows, players)
                    if state is not None:
                        state.put_rows(match_id, kept)
                first, body, count = kept
                match_rows.append(first)
                main.write(body)
                totals["matches"] += 1
                totals["rows"] += count
    if state is not None:
        state.flush()
        state.audit_rows(lambda ids: _fresh_outputs(ids, players))

    matches = matches_file(pd.concat(match_rows, ignore_index=True))
    json_tables = read_json_tables(list(SOURCES))
    facts = json_tables["facts"]
    facts["match_id"] = pd.to_numeric(facts["match_id"]).astype("Int64")
    matches["match_id"] = matches["match_id"].astype("Int64")
    matches = matches.merge(facts[["match_id", *MATCH_FACT_COLUMNS]], on="match_id", how="left")
    for column in MATCH_WHOLE_NUMBER_COLUMNS:
        if column in matches.columns:
            matches[column] = whole_if_possible(matches[column])
    matches.to_csv(out / "matches.csv", index=False)
    totals["json_files"] = write_json_files(out, json_tables)
    register_path = cricsheet_dir() / "people.csv"
    register = pd.read_csv(register_path, dtype=str) if register_path.exists() else pd.DataFrame(columns=["identifier"])
    in_dataset = set(json_tables["people"]["person_id"].dropna()) | set(players["identifier"])
    external = external_ids_file(register, in_dataset)
    external.to_csv(out / "player_external_ids.csv", index=False)
    totals["json_files"]["player_external_ids.csv"] = len(external)
    players_file(players).to_csv(out / "players.csv", index=False)
    fetched = cricsheet_dir() / "fetched.json"
    snapshot_date = json.loads(fetched.read_text())["snapshot_date"] if fetched.exists() else None
    (out / "manifest.json").write_text(json.dumps({"snapshot_date": snapshot_date}, indent=1), encoding="utf-8")
    return totals
