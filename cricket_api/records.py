"""Team records and head-to-head records, counted from the match table alone.

A match is one row of the match table, so it is counted once however many innings it had and whether or not it went to a Super Over. The innings, balls and wickets are
never read here.

A team is its name and the gender of the cricket (the same name is used in men's and women's cricket). A renamed team stays separate teams, as recorded. For one team in
one match the result is exactly one of these, from the winner and outcome fields:

  win         the winner is the team. This includes Duckworth-Lewis wins and awarded wins.
  loss        a winner is recorded and it is the other team.
  draw        the outcome is draw (Tests).
  tie         the outcome is tie. A tie is neither a win nor a loss, and a Super Over or bowl-out that settled it does not change that.
  no result   the outcome is no result. This includes matches abandoned after play began, because the data does not tell them apart.

A recorded tie keeps its eliminator beside it: the team Cricsheet names as the eliminator winner gets ties_won_eliminator and the other team ties_lost_eliminator, and a tie with no
eliminator named is ties_no_eliminator. The 2019 World Cup final is a tie for both teams with England the eliminator winner, and is never a win.

  matches = wins + losses + draws + ties + no_results
  ties = ties_won_eliminator + ties_lost_eliminator + ties_no_eliminator

decisive_win_percentage is wins over wins plus losses, to two places. Draws, ties and no results are left out of that denominator, and it is null when there is none.

Every record is for the matches in this dataset, not the complete history of the teams.
"""
from datetime import date

from cricket_api.errors import ApiError

SCOPE = "this dataset"
COVERAGE_NOTE = "Counted from the Cricsheet matches in this dataset only. These are not complete historical records. See /v1/meta for the served data's coverage."
SPLIT_DIMENSIONS = {"opponent": "opponent", "venue": "venue", "competition": "collection", "format": "format", "season": "season"}
HEAD_TO_HEAD_DIMENSIONS = ("venue", "competition", "format", "season")

RESULT = """CASE WHEN m.winner = ? THEN 'win' WHEN m.winner IS NOT NULL THEN 'loss' WHEN m.outcome = 'draw' THEN 'draw' WHEN m.outcome = 'tie' THEN 'tie'
          WHEN m.outcome = 'no result' THEN 'no_result' ELSE 'unknown' END"""
ELIMINATOR = "CASE WHEN m.outcome <> 'tie' THEN NULL WHEN m.eliminator = ? THEN 'won' WHEN m.eliminator IS NOT NULL THEN 'lost' ELSE 'none' END"
TOTALS = """count(*) AS matches, count(*) FILTER (WHERE result = 'win') AS wins, count(*) FILTER (WHERE result = 'loss') AS losses, count(*) FILTER (WHERE result = 'draw') AS draws,
       count(*) FILTER (WHERE result = 'tie') AS ties, count(*) FILTER (WHERE result = 'no_result') AS no_results,
       count(*) FILTER (WHERE result = 'tie' AND elim = 'won') AS ties_won_eliminator, count(*) FILTER (WHERE result = 'tie' AND elim = 'lost') AS ties_lost_eliminator,
       count(*) FILTER (WHERE result = 'tie' AND elim = 'none') AS ties_no_eliminator, count(*) FILTER (WHERE result = 'unknown') AS unknown,
       min(start_date) AS first_match, max(start_date) AS last_match"""


def team_exists(db, team: str, gender: str) -> bool:
    """Whether the team name has played any match of that gender. This is checked before any statistical filter, so a team the data does not know is a 404 and a known team
    with nothing inside the filters is zeros."""
    return bool(db.query("SELECT 1 FROM matches WHERE gender = ? AND (team_1 = ? OR team_2 = ?) LIMIT 1", [gender, team, team]))


def require_team(db, team: str, gender: str, parameter: str) -> None:
    if not team_exists(db, team, gender):
        raise ApiError(404, "Team not found for that gender", parameter=parameter)


def record_stats(row: dict) -> dict:
    """The record of one set of matches, from its counts. Raises if the counts do not add up, which would be a fault in the data or the code and never a reply."""
    stats = {key: int(row[key]) for key in ("matches", "wins", "losses", "draws", "ties", "no_results", "ties_won_eliminator", "ties_lost_eliminator", "ties_no_eliminator")}
    if row["unknown"] or stats["matches"] != stats["wins"] + stats["losses"] + stats["draws"] + stats["ties"] + stats["no_results"]:
        raise RuntimeError("a match has a result the rules do not cover")
    if stats["ties"] != stats["ties_won_eliminator"] + stats["ties_lost_eliminator"] + stats["ties_no_eliminator"]:
        raise RuntimeError("the ties do not add up")
    decisive = stats["wins"] + stats["losses"]
    return {**stats, "decisive_win_percentage": round(100 * stats["wins"] / decisive, 2) if decisive else None}


def coverage(row: dict) -> dict:
    """What the whole filtered result covers: this dataset, how many matches and the dates of the first and last of them. Null dates when there are none."""
    return {"scope": SCOPE, "matches": int(row["matches"]), "first_match": row["first_match"], "last_match": row["last_match"], "note": COVERAGE_NOTE}


def perspective(matches_sql: str) -> str:
    """The matches of one team with its result and its opponent in each. The parameters are the team three times: for the opponent, the result and the eliminator."""
    return (f"WITH matches_of AS ({matches_sql}), tm AS (SELECT m.*, CASE WHEN m.team_1 = ? THEN m.team_2 ELSE m.team_1 END AS opponent, {RESULT} AS result, "
            f"{ELIMINATOR} AS elim FROM matches_of m)")


def selected(gender: str, team: str, opponent: str | None, collection, format, season, date_from, date_to) -> tuple[str, list]:
    """The matches of the team, or of the pairing in either order, that the filters pick, and their parameters."""
    conditions, params = ["gender = ?"], [gender]
    for column, value in (("collection", collection), ("format", format), ("season", season)):
        if value is not None:
            conditions.append(f"{column} = ?")
            params.append(value)
    if date_from is not None:
        conditions.append("start_date >= ?")
        params.append(date_from)
    if date_to is not None:
        conditions.append("start_date <= ?")
        params.append(date_to)
    if opponent is None:
        conditions.append("(team_1 = ? OR team_2 = ?)")
        params += [team, team]
    else:
        conditions.append("((team_1 = ? AND team_2 = ?) OR (team_1 = ? AND team_2 = ?))")
        params += [team, opponent, opponent, team]
    return f"SELECT match_id, season, start_date, venue, collection, format, team_1, team_2, winner, outcome, eliminator FROM matches WHERE {' AND '.join(conditions)}", params


def overall(db, matches_sql: str, params: list, team: str) -> dict:
    row = db.query(f"{perspective(matches_sql)} SELECT {TOTALS} FROM tm", [*params, team, team, team])[0]
    return {"record": record_stats(row), "coverage": coverage(row)}


def groups(db, matches_sql: str, params: list, team: str, dimension: str) -> list[dict]:
    """The record of the team in each group of the matches, with each group's own counts. The dimension is chosen from the allow-list, never from the request."""
    column = SPLIT_DIMENSIONS[dimension]
    rows = db.query(f"{perspective(matches_sql)} SELECT {column} AS gkey, {TOTALS} FROM tm GROUP BY {column}", [*params, team, team, team])
    return [{"key": r["gkey"], **record_stats(r)} for r in rows]


def split_label(dimension: str, key):
    from cricket_api import metrics
    return metrics.split_label(dimension, key) if dimension in ("competition", "format") else key
