"""Player profile columns from the frozen snapshot, and the match and player files built from the Cricsheet rows.

The snapshot (inputs/player_profiles.csv) is a one-time copy of the profile columns the live data was built with. It is made by
snapshot.py, the only code that reads the repository's data/ folder, and it is not refreshed by the weekly flow. The exact date of birth
is kept in the snapshot only to compute the age at each match. It is never written to any export.
"""
import re

import pandas as pd

# Short columns stay on every ball row. The same set as the dataset the live data came from.
ON_EVERY_ROW = ["country", "batting style", "bowling style", "playing role"]
MATCH_ON_EVERY_ROW = ["season", "start_date", "gender", "event", "format", "type"]
MATCH_FILE_ONLY = ["venue", "match_number", "toss_winner", "toss_decision", "player_of_match", "player_of_match_2",
                   "winner", "winner_runs", "winner_wickets", "outcome"]
SNAPSHOT_COLUMNS = ["identifier", "name", "country", "batting style", "bowling style", "playing role", "birth_date"]


def load_snapshot(path) -> pd.DataFrame:
    table = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    missing = [c for c in SNAPSHOT_COLUMNS if c not in table.columns]
    if missing:
        raise ValueError(f"{path} lacks the columns {missing}")
    if table["identifier"].duplicated().any():
        raise ValueError("a Cricsheet identifier appears twice in the profile snapshot")
    table["birth_date"] = pd.to_datetime(table["birth_date"], errors="coerce")
    return table[SNAPSHOT_COLUMNS]


def age_in_years(birth_date: pd.Series, match_date: pd.Series) -> pd.Series:
    """Whole years of age on the first day of the match. Empty when either date is missing."""
    birth_date = pd.to_datetime(birth_date)
    match_date = pd.to_datetime(match_date)
    years = match_date.dt.year - birth_date.dt.year
    had_birthday = (match_date.dt.month > birth_date.dt.month) | ((match_date.dt.month == birth_date.dt.month) & (match_date.dt.day >= birth_date.dt.day))
    return (years - (~had_birthday).astype(int)).astype("Int64")


def attach_players(rows: pd.DataFrame, players: pd.DataFrame) -> pd.DataFrame:
    """Add the short player columns and the age at the match for striker and bowler. Keeps row order and the row count."""
    result = rows
    for role in ("striker", "bowler"):
        wanted = players[["identifier", "birth_date", *ON_EVERY_ROW]]
        renamed = wanted.rename(columns={"identifier": f"{role}_id", "birth_date": f"birth_date_{role}", **{c: f"{c}_{role}" for c in ON_EVERY_ROW}})
        result = result.merge(renamed, on=f"{role}_id", how="left")
        result[f"{role}_age"] = age_in_years(result[f"birth_date_{role}"], result["start_date"])
        result = result.drop(columns=[f"birth_date_{role}"])       # the exact birth date never reaches a file that is written out
    if len(result) != len(rows):
        raise ValueError("joining players changed the number of rows")
    return result


def players_file(players: pd.DataFrame) -> pd.DataFrame:
    """One row per player in the snapshot, joined to the ball rows on player_id (the same value as striker_id and bowler_id)."""
    columns = ["identifier", "name", "country", "batting style", "bowling style", "playing role"]
    return players[columns].rename(columns={"identifier": "player_id"}).sort_values("player_id").reset_index(drop=True)


def matches_file(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per match with every match-level column, including the ones kept on the ball rows."""
    columns = ["match_id", *MATCH_ON_EVERY_ROW, *MATCH_FILE_ONLY]
    per_match = rows[columns].drop_duplicates()
    if per_match["match_id"].duplicated().any():
        raise ValueError("a match has two different values for a match-level column")
    return per_match.sort_values("match_id").reset_index(drop=True)


def without_match_file_columns(rows: pd.DataFrame) -> pd.DataFrame:
    return rows.drop(columns=MATCH_FILE_ONLY)


def external_ids_file(register: pd.DataFrame, wanted: set[str]) -> pd.DataFrame:
    """One row per id a person has in another system, for the people in the data, from Cricsheet's register. Cricinfo ids are left out."""
    people = register[register["identifier"].isin(wanted)]
    rows = []
    for column in register.columns:
        if not column.startswith("key_") or "cricinfo" in column:
            continue
        found = people[["identifier", column]].dropna()
        source = re.sub(r"_\d+$", "", column[len("key_"):])
        rows.append(pd.DataFrame({"player_id": found["identifier"], "source": source, "external_id": found[column].astype(str).str.replace(r"\.0$", "", regex=True)}))
    table = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=["player_id", "source", "external_id"])
    return table.drop_duplicates().sort_values(["player_id", "source", "external_id"]).reset_index(drop=True)
