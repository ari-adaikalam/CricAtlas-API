"""Leaderboards: who is first at a metric, over the matches the filters select.

Totals come from metrics.py, the same rules as the career and season routes, summed over every player. Then:

  Eligibility. A ratio needs a sample, a count does not. A batting average and a strike rate need min_balls balls faced, and an average also needs min_dismissals
  dismissals. A bowling average and strike rate need min_balls balls bowled and min_wickets wickets, and economy needs min_balls. Each metric has a default, the caller
  may change any threshold (1 or more), and the thresholds that were applied come back in the response. Eligibility is judged on the totals of the requested filters,
  so a player can qualify for one competition and not for another. There is no minimum of runs, because that would pick players by how well they did and not by how much
  they played. The defaults are product choices, not an official qualification rule.
  Players with a null ratio (no dismissals, no wickets) are left out. A count metric leaves out players on zero, with one exception: a highest score of 0 is a genuine
  score (a player who batted and made nothing), so it is listed, last, and a player who never batted has no highest score at all and is left out.
  Order. Ratios are ranked on the unrounded value and only the shown value is rounded to 2 places. Batting average and strike rate go high to low. Bowling average, economy
  and strike rate go low to high. Best figures are most wickets, then fewest runs. Ties in the value go to the player with more balls (faced or bowled), then more matches,
  then the smaller player id, so the order never depends on how the data was read. rank is the position in the list.

The metrics, their order and their SQL are chosen from the allow-lists below, never from the request.
"""
from typing import NamedTuple

from cricket_api import metrics

MAX_LIMIT = 100
MAX_THRESHOLD = 1_000_000


class Metric(NamedTuple):
    value: str                    # SQL that ranks by it, on the unrounded value
    order: str                    # DESC or ASC
    kind: str                     # "count" or "ratio"
    min_balls: int | None         # default thresholds
    min_other: int | None         # dismissals for batting, wickets for bowling
    shown: str | None = None      # SQL for the value that is shown, when it differs from the ranking value
    extra_ties: str = ""          # tie-breakers before the sample size
    need: str = "TRUE"            # a condition the row must meet


BATTING = {
    "runs": Metric("runs", "DESC", "count", None, None, need="runs > 0"),
    "average": Metric("CASE WHEN dismissals > 0 THEN runs * 1.0 / dismissals END", "DESC", "ratio", 500, 20),
    "strike_rate": Metric("CASE WHEN balls_faced > 0 THEN runs * 100.0 / balls_faced END", "DESC", "ratio", 500, None),
    "hundreds": Metric("hundreds", "DESC", "count", None, None, need="hundreds > 0"),
    "fifties": Metric("fifties", "DESC", "count", None, None, need="fifties > 0"),
    "fours": Metric("fours", "DESC", "count", None, None, need="fours > 0"),
    "sixes": Metric("sixes", "DESC", "count", None, None, need="sixes > 0"),
    "highest_score": Metric("highest_score", "DESC", "count", None, None, need="highest_score IS NOT NULL", extra_ties="CASE WHEN highest_score_not_out THEN 0 ELSE 1 END,"),
}
BOWLING = {
    "wickets": Metric("wickets", "DESC", "count", None, None, need="wickets > 0"),
    "maidens": Metric("maidens", "DESC", "count", None, None, need="maidens > 0"),
    "five_wicket_hauls": Metric("five_wicket_hauls", "DESC", "count", None, None, need="five_wicket_hauls > 0"),
    "average": Metric("CASE WHEN wickets > 0 THEN runs * 1.0 / wickets END", "ASC", "ratio", 300, 10),
    "economy": Metric("CASE WHEN balls > 0 THEN runs * 6.0 / balls END", "ASC", "ratio", 300, None),
    "strike_rate": Metric("CASE WHEN wickets > 0 THEN balls * 1.0 / wickets END", "ASC", "ratio", 300, 10),
    "best_figures": Metric(f"best_wickets * 100000 - best_runs", "DESC", "count", None, None, shown="best_wickets", need="best_wickets >= 1"),
}


def ranking_sql(definitions: dict, metric: str, sample: str, other: str, min_balls: int | None, min_other: int | None) -> tuple[str, list]:
    """The SELECT that ranks the `totals` relation, and its parameters. `sample` is the column of balls (balls_faced or balls) and `other` the second threshold column."""
    chosen = definitions[metric]                       # a KeyError here means a metric outside the allow-list
    conditions, params = ["TRUE"], []
    if min_balls is not None:
        conditions.append(f"t.{sample} >= ?")
        params.append(min_balls)
    if min_other is not None:
        conditions.append(f"t.{other} >= ?")
        params.append(min_other)
    shown = chosen.shown or "value"
    ties = f"{chosen.extra_ties} {sample} DESC, matches DESC, pid ASC"
    sql = (f"SELECT row_number() OVER (ORDER BY value {chosen.order}, {ties}) AS rank, pid, name, {shown} AS shown, * EXCLUDE (name) FROM ("
           f"SELECT t.*, p.name, {chosen.value} AS value FROM totals t JOIN players p ON p.player_id = t.pid WHERE {' AND '.join(conditions)}) "
           f"WHERE value IS NOT NULL AND {chosen.need} ORDER BY value {chosen.order}, {ties} LIMIT ?")
    return sql, params


def rank(db, kind: str, matches_sql: str, match_params: list, metric: str, limit: int, min_balls: int | None, min_other: int | None) -> list[dict]:
    """The leaders as rows of totals, in order. kind is batting or bowling."""
    if kind == "batting":
        definitions, relation, sample, other = BATTING, metrics.batting_all(matches_sql), "balls_faced", "dismissals"
    elif kind == "bowling":
        definitions, relation, sample, other = BOWLING, metrics.bowling_all(matches_sql), "balls", "wickets"
    else:
        raise ValueError(kind)
    select, params = ranking_sql(definitions, metric, sample, other, min_balls, min_other)
    rows = db.query(f"{relation} {select}", [*match_params, *params, limit])
    chosen = definitions[metric]
    for row in rows:
        row["shown"] = round(row["shown"], 2) if chosen.kind == "ratio" else int(row["shown"])
    return rows


def defaults(kind: str, metric: str) -> tuple[int | None, int | None]:
    chosen = (BATTING if kind == "batting" else BOWLING)[metric]
    return chosen.min_balls, chosen.min_other
