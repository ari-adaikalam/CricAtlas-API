"""Turn one Cricsheet match into dataset rows.

Cricsheet supplies the facts of each ball. This module adds the running totals for the striker and for a
dismissed player, and splits Cricsheet's over.ball number into columns that cannot be misread.
"""
import csv

import pandas as pd

from cricket_refresh.config import Source

INFO_FIELDS = [
    "gender", "event", "match_number", "toss_winner", "toss_decision",
    "player_of_match", "winner", "winner_runs", "winner_wickets", "outcome",
]
NUMERIC_INFO_FIELDS = ("match_number", "winner_runs", "winner_wickets")

RAW_COLUMNS = [
    "match_id", "season", "start_date", "venue", "innings", "ball",
    "batting_team", "bowling_team", "striker", "non_striker", "bowler",
    "runs_off_bat", "extras", "wides", "noballs", "byes", "legbyes", "penalty",
    "wicket_type", "player_dismissed", "other_wicket_type", "other_player_dismissed",
    "actual_delivery",
]

ID_COLUMNS = ["striker_id", "bowler_id"]

# Column order of the dataset, for the part that comes from Cricsheet.
# 'player_of_match_2' is the second award line when a match has joint winners.
CRICSHEET_COLUMNS = [
    "match_id", "season", "start_date", "venue", "innings", "ball", "over", "ball_in_over", "scorecard_ball", "is_legal_delivery",
    "batting_team", "bowling_team", "striker", "striker_id", "non_striker", "bowler", "bowler_id",
    "runs_off_bat", "extras", "wides", "noballs", "byes", "legbyes", "penalty",
    "wicket_type", "player_dismissed", "other_wicket_type", "other_player_dismissed",
    "wicket", "striker balls faced", "total striker runs",
    "player out runs", "player out balls faced",
    "gender", "event", "match_number", "toss_winner", "toss_decision",
    "player_of_match", "player_of_match_2", "winner", "winner_runs",
    "winner_wickets", "outcome", "format", "type",
    "fielder_1", "fielder_1_id", "fielder_1_substitute", "non_boundary",
]

# The columns of the published ball and match rows that come from Cricsheet, which `verify` rebuilds and compares
PUBLISHED_COLUMNS = [column for column in CRICSHEET_COLUMNS if column not in ID_COLUMNS]
FIELDER_COLUMNS = ["fielder_1", "fielder_1_id", "fielder_1_substitute"]


def _clean(text: str) -> str:
    return text.strip().replace('"', "")


def _numeric_if_possible(value):
    """Keep a value as text if it is not a plain number, so nothing is silently dropped."""
    if value is None:
        return None
    number = pd.to_numeric(value, errors="coerce")
    return value if pd.isna(number) else float(number)


def parse_info(lines: list[str]) -> dict:
    """Pull the wanted fields out of a Cricsheet info file.

    Lines look like 'info,toss_winner,India'. The value can contain commas, so a
    line is split into at most three parts.
    """
    found: dict[str, list[str]] = {}
    for line in lines:
        parts = line.strip().split(",", 2)
        while len(parts) < 3:
            parts.append("")
        attribute = _clean(parts[1])
        if attribute in INFO_FIELDS:
            found.setdefault(attribute, []).append(_clean(parts[2]))

    info = {}
    for field in INFO_FIELDS:
        values = found.get(field, [])
        first = values[0] if values and values[0] != "" else None
        info[field] = _numeric_if_possible(first) if field in NUMERIC_INFO_FIELDS else first
    awards = found.get("player_of_match", [])
    info["player_of_match_2"] = awards[1] if len(awards) > 1 and awards[1] != "" else None
    return info


def parse_registry(lines: list[str]) -> dict[str, str]:
    """Map each player name in a match to their Cricsheet identifier.

    The csv module is used because a name containing a comma is quoted.
    """
    registry = {}
    for row in csv.reader(lines):
        if len(row) >= 5 and row[1] == "registry" and row[2] == "people":
            registry[row[3]] = row[4]
    return registry


def add_running_stats(balls: pd.DataFrame) -> pd.DataFrame:
    """Add wicket flag, balls faced and runs for the striker, and the same for a dismissed player.

    The totals are for the innings and start again at 0 when a new innings starts, so a Super Over
    starts from nothing and a Test batter's second innings does not add the first. A wide is not a
    ball faced and does not count. A no-ball, a bye and a leg bye do. A dismissed non-striker (run
    out) gets the totals they had so far.
    """
    faced: dict = {}
    runs: dict = {}
    out_faced: dict = {}
    out_runs: dict = {}
    innings_now = None

    wide = pd.to_numeric(balls["wides"], errors="coerce").fillna(0) > 0
    striker_faced, striker_runs, dismissed_runs, dismissed_faced = [], [], [], []
    for striker, scored, dismissed, innings, is_wide in zip(
        balls["striker"], balls["runs_off_bat"], balls["player_dismissed"], balls["innings"], wide
    ):
        if innings != innings_now:
            faced, runs, out_faced, out_runs = {}, {}, {}, {}
            innings_now = innings
        if not is_wide:
            faced[striker] = faced.get(striker, 0) + 1
        else:
            faced.setdefault(striker, 0)
        runs[striker] = runs.get(striker, 0) + scored
        is_out = pd.notna(dismissed)
        if is_out:
            out_runs[dismissed] = runs.get(dismissed, 0)
            out_faced[dismissed] = faced.get(dismissed, 0)
        striker_faced.append(faced[striker])
        striker_runs.append(runs[striker])
        dismissed_runs.append(out_runs[dismissed] if is_out else None)
        dismissed_faced.append(out_faced[dismissed] if is_out else None)

    result = balls.copy()
    result["wicket"] = balls["player_dismissed"].notna().astype(int)
    result["striker balls faced"] = pd.Series(striker_faced, index=balls.index, dtype="float64")
    result["total striker runs"] = pd.Series(striker_runs, index=balls.index, dtype="float64")
    result["player out runs"] = pd.Series(dismissed_runs, index=balls.index, dtype="float64")
    result["player out balls faced"] = pd.Series(dismissed_faced, index=balls.index, dtype="float64")
    return result


def split_ball(ball) -> tuple:
    """Cricsheet's ball is over.ball written as text, such as 4.1 or 4.10. Read as a number the tenth delivery
    of an over looks like the first, so the over and the position in the over are taken from the text.
    Returns (over, ball_in_over), counting every delivery, wides and no balls included."""
    over, _, position = str(ball).partition(".")
    return (int(over), int(position)) if over.isdigit() and position.isdigit() else (None, None)


def add_ball_columns(rows: pd.DataFrame) -> pd.DataFrame:
    """Add over, ball_in_over, scorecard_ball and is_legal_delivery, and make ball and actual_delivery plain numbers.

    over and ball_in_over come from the text of ball. scorecard_ball is Cricsheet's actual_delivery, the ball
    as a scorecard numbers it, where a wide or no ball has the number of the legal ball it comes with.
    is_legal_delivery is False for a wide or a no ball. Their columns are blank when there is none, so a
    comparison with 0 would match nothing.
    """
    parts = [split_ball(b) for b in rows["ball"]]
    result = rows.copy()
    result["over"] = pd.Series([p[0] for p in parts], index=rows.index, dtype="Int64")
    result["ball_in_over"] = pd.Series([p[1] for p in parts], index=rows.index, dtype="Int64")
    result["scorecard_ball"] = pd.to_numeric(rows["actual_delivery"], errors="coerce")
    illegal = (pd.to_numeric(rows["wides"], errors="coerce").fillna(0) > 0) | (pd.to_numeric(rows["noballs"], errors="coerce").fillna(0) > 0)
    result["is_legal_delivery"] = ~illegal
    result["ball"] = pd.to_numeric(rows["ball"], errors="coerce")
    return result


def add_fielder_columns(rows: pd.DataFrame, first_fielders: dict | None = None) -> pd.DataFrame:
    """Add the first listed fielder of the first wicket. first_fielders maps (innings, delivery number) to the three values.

    The delivery number is the n-th row of the innings. Rows with no fielder, and every row when none are given, stay empty.
    """
    result = rows.copy()
    numbers = result.groupby("innings").cumcount() + 1
    found = first_fielders or {}
    keys = list(zip(result["innings"], numbers))
    for column in FIELDER_COLUMNS:
        result[column] = pd.Series([found.get(k, {}).get(column) for k in keys], index=result.index, dtype="object")
    result["fielder_1_substitute"] = result["fielder_1_substitute"].astype("boolean")
    return result


def build_match_rows(balls: pd.DataFrame, info_lines: list[str], source: Source, first_fielders: dict | None = None) -> pd.DataFrame:
    missing = [column for column in RAW_COLUMNS if column not in balls.columns]
    if missing:
        raise ValueError(f"Cricsheet match file is missing columns: {missing}")

    rows = add_ball_columns(add_running_stats(balls[RAW_COLUMNS]))
    # Cricsheet marks a four or six that was run and not hit to the boundary. A blank means it was not marked, so False and never empty.
    rows["non_boundary"] = balls["non_boundary"].notna().to_numpy() if "non_boundary" in balls.columns else False
    registry = parse_registry(info_lines)
    rows["striker_id"] = rows["striker"].map(registry)
    rows["bowler_id"] = rows["bowler"].map(registry)
    for field, value in parse_info(info_lines).items():
        rows[field] = value
    rows["format"] = source.format
    rows["type"] = source.type
    return add_fielder_columns(rows, first_fielders)[CRICSHEET_COLUMNS]
