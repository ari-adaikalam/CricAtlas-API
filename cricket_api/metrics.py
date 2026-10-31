"""Batting, bowling and fielding totals, written once for the career routes and the season routes.

Every total is a sum, and every rate is made from summed numerators and denominators: a season's average is its runs over its dismissals, and a career average is
the career's runs over the career's dismissals. A rate is never the mean of other rates. A row with a zero denominator has a null rate.

The rules are the scorecard's (scorecard.py) and the maiden rule's (maidens.py), plus these:

  A Super Over is not part of a career or a season: its runs, balls, wickets and innings are left out, as official records do.
  Innings batted: the player faced a ball, stood at the non-striker's end for one, or was dismissed.
  Not out: an innings in which the player was not dismissed. Retired hurt and retired not out count as not out. Retired out is a dismissal.
  Highest score: the most runs in one innings, and a not out score beats an equal out score.
  Average: runs per dismissal. Strike rate: runs per 100 balls faced.
  Bowling: balls are legal deliveries. Economy is runs per six balls in every format. Strike rate is balls per wicket. Best figures are the most wickets in an
  innings, then the fewest runs. Maidens come from maidens.py, so a five-ball Hundred set and a miscounted over the umpires counted both work.
  Fielding counts distinct wickets per player: catches by a fielder, caught and bowled (the catcher is the bowler, and these wickets carry no fielder row),
  stumpings, and run out involvements (every named fielder gets one, which is not a direct hit). A substitute fielder's wickets are counted apart as substitute
  involvements and are in none of the other counts.

A dimension is chosen from GROUPS, never from the request, so no request text reaches the SQL. A season is Cricsheet's season label ("2019" or "2019/20"), and
a group is ordered by the date of its first match, then by its label.
"""
from cricket_api.maidens import MAIDEN_OVERS
from cricket_api.scorecard import CREDITED, NOT_A_WICKET, rate

GROUPS = {"career": None, "season": "season", "opponent": "opponent", "venue": "venue", "competition": "collection", "format": "format"}
SPLIT_COLUMNS = {"opponent", "venue", "collection", "format"}
COLLECTION_LABELS = {"tests": "Tests", "odis": "ODIs", "t20s": "T20Is", "ipl": "IPL", "wpl": "WPL", "bbl": "BBL", "wbb": "WBBL", "cpl": "CPL", "psl": "PSL", "sat": "SA20",
                     "ilt": "ILT20", "hnd": "The Hundred", "mlc": "MLC", "lpl": "LPL", "bpl": "BPL"}
FORMAT_LABELS = {"TEST": "Test", "ODI": "ODI", "T20": "T20"}
CREDITED_SQL = ", ".join(f"'{k}'" for k in sorted(CREDITED))
NOT_A_WICKET_SQL = ", ".join(f"'{k}'" for k in sorted(NOT_A_WICKET))
# The most wickets, then the fewest runs, as one number to take a maximum of. No innings concedes 100,000 runs.
BEST_FIGURES = "wickets * 100000 - runs"


def selected_matches(collection, format, gender, season, date_from, date_to) -> tuple[str, list]:
    """The matches the filters pick, as a SQL subquery and its parameters."""
    conditions, params = [], []
    for column, value in (("collection", collection), ("format", format), ("gender", gender), ("season", season)):
        if value is not None:
            conditions.append(f"{column} = ?")
            params.append(value)
    if date_from is not None:
        conditions.append("start_date >= ?")
        params.append(date_from)
    if date_to is not None:
        conditions.append("start_date <= ?")
        params.append(date_to)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return f"SELECT match_id, season, start_date, venue, collection, format, coalesce(balls_per_over, 6) AS per_over FROM matches {where}", params


def grouping(by: str | None) -> tuple[str, str, str]:
    """(columns to select, GROUP BY clause, ORDER BY clause) for a dimension from the allow-list. A season has dates, a split has a key, and the career has neither."""
    if by not in GROUPS.values():
        raise ValueError(f"not an allowed dimension: {by!r}")
    if by is None:
        return "", "", ""
    if by == "season":
        return "season, min(start_date) AS first_match, max(start_date) AS last_match,", "GROUP BY season", "ORDER BY min(start_date), season"
    return f"{by} AS gkey,", f"GROUP BY {by}", ""


def with_group(row: dict, stats: dict, by: str | None) -> dict:
    if by == "season":
        return {"season": row["season"], "first_match": row["first_match"], "last_match": row["last_match"], **stats}
    return {"key": row["gkey"], **stats} if by in SPLIT_COLUMNS else stats


def batting_stats(row: dict) -> dict:
    runs, balls = int(row["runs"]), int(row["balls_faced"])
    return {"matches": row["matches"], "innings": row["innings"], "not_outs": row["not_outs"], "runs": runs, "balls_faced": balls, "highest_score": row["highest_score"],
            "highest_score_not_out": row["highest_score_not_out"], "average": round(runs / row["dismissals"], 2) if row["dismissals"] else None,
            "strike_rate": rate(runs, balls, 6, 100), "fifties": row["fifties"], "hundreds": row["hundreds"], "ducks": row["ducks"], "fours": int(row["fours"]), "sixes": int(row["sixes"])}


BATTING_TOTALS = """count(DISTINCT match_id) AS matches, count(*) AS innings, count(*) FILTER (WHERE NOT is_out) AS not_outs, coalesce(sum(runs), 0) AS runs,
           coalesce(sum(balls), 0) AS balls_faced, max(runs) AS highest_score,
           arg_max(NOT is_out, runs * 2 + CASE WHEN is_out THEN 0 ELSE 1 END) AS highest_score_not_out,
           count(*) FILTER (WHERE is_out) AS dismissals, count(*) FILTER (WHERE runs >= 50 AND runs < 100) AS fifties, count(*) FILTER (WHERE runs >= 100) AS hundreds,
           count(*) FILTER (WHERE runs = 0 AND is_out) AS ducks, coalesce(sum(fours), 0) AS fours, coalesce(sum(sixes), 0) AS sixes"""


def batting_innings(one_player: bool, by: str | None = None) -> str:
    """The WITH clauses that give one row per innings batted for every player (`played`), or for the one player whose id is passed five times. `m` must be defined."""
    only = " AND {} = ?" if one_player else ""
    # the opponent of a batter is the side that bowled in that innings, read from the balls of the innings the player batted in
    sides = """
    sides AS (SELECT b.match_id, b.innings, any_value(b.bowling_team) AS bowling_team FROM balls b
              WHERE (b.match_id, b.innings) IN (SELECT match_id, innings FROM appearance) GROUP BY b.match_id, b.innings),""" if by == "opponent" else ""
    opponent = "s.bowling_team" if by == "opponent" else "CAST(NULL AS VARCHAR)"
    join_sides = "LEFT JOIN sides s ON s.match_id = a.match_id AND s.innings = a.innings" if by == "opponent" else ""
    return f"""
    reg AS (SELECT match_id, innings FROM innings WHERE NOT is_super_over AND match_id IN (SELECT match_id FROM m)),
    appearance AS (
        SELECT b.match_id, b.innings, b.striker_id AS pid FROM balls b JOIN reg r ON r.match_id = b.match_id AND r.innings = b.innings
            WHERE b.striker_id IS NOT NULL{only.format("b.striker_id") if one_player else ""}
        UNION
        SELECT b.match_id, b.innings, p.person_id AS pid FROM balls b JOIN reg r ON r.match_id = b.match_id AND r.innings = b.innings
            JOIN match_people p ON p.match_id = b.match_id AND p.person = b.non_striker AND p.team = b.batting_team AND p.role = 'player'
            WHERE p.person_id IS NOT NULL{only.format("p.person_id") if one_player else ""}
        UNION
        SELECT w.match_id, w.innings, w.player_dismissed_id AS pid FROM wickets w JOIN reg r ON r.match_id = w.match_id AND r.innings = w.innings
            WHERE w.player_dismissed_id IS NOT NULL{only.format("w.player_dismissed_id") if one_player else ""}),
    faced AS (
        SELECT match_id, innings, striker_id AS pid, sum(coalesce(runs_off_bat, 0)) AS runs, count(*) FILTER (WHERE coalesce(wides, 0) = 0) AS balls,
               count(*) FILTER (WHERE runs_off_bat = 4 AND NOT non_boundary) AS fours, count(*) FILTER (WHERE runs_off_bat = 6 AND NOT non_boundary) AS sixes
        FROM balls JOIN reg USING (match_id, innings) WHERE striker_id IS NOT NULL{only.format("striker_id") if one_player else ""} GROUP BY match_id, innings, striker_id),
    dismissed AS (
        SELECT DISTINCT match_id, innings, player_dismissed_id AS pid FROM wickets JOIN reg USING (match_id, innings)
        WHERE player_dismissed_id IS NOT NULL AND wicket_type NOT IN ({NOT_A_WICKET_SQL}){only.format("player_dismissed_id") if one_player else ""}),{sides}
    played AS (
        SELECT a.pid, a.match_id, a.innings, m.season, m.start_date, m.venue, m.collection, m.format, {opponent} AS opponent, coalesce(f.runs, 0) AS runs, coalesce(f.balls, 0) AS balls,
               coalesce(f.fours, 0) AS fours, coalesce(f.sixes, 0) AS sixes, d.match_id IS NOT NULL AS is_out
        FROM appearance a JOIN m ON m.match_id = a.match_id LEFT JOIN faced f ON f.match_id = a.match_id AND f.innings = a.innings AND f.pid = a.pid
        LEFT JOIN dismissed d ON d.match_id = a.match_id AND d.innings = a.innings AND d.pid = a.pid {join_sides})"""


def batting_played_table(by: str | None = None, one_player: bool = False) -> str:
    """The `played` relation read from player_batting_innings (every player, or the one whose id is the next parameter) joined to the selected matches `m`. The table stores the opponent (the side that bowled in
    the innings, read from the balls as the definition in batting_innings reads it), so it is exact by construction; the leaderboards do not need it and get a NULL."""
    opponent = "p.opponent" if by == "opponent" else "CAST(NULL AS VARCHAR)"
    where = " WHERE p.player_id = ?" if one_player else ""
    return (f"played AS (SELECT p.player_id AS pid, p.match_id, p.innings, m.season, m.start_date, m.venue, m.collection, m.format, {opponent} AS opponent, "
            f"p.runs, p.balls, p.fours, p.sixes, p.is_out FROM player_batting_innings p JOIN m ON m.match_id = p.match_id{where})")


def batting(db, matches_sql: str, params: list, player_id: str, by: str | None = None) -> list[dict]:
    """A player's batting totals (career, or grouped by season or a split dimension), from the per-innings table. batting_from_balls is the definition it must equal."""
    head, group, order = grouping(by)
    sql = f"WITH m AS ({matches_sql}), {batting_played_table(by, True)} SELECT {head} {BATTING_TOTALS} FROM played {group} {order}"
    return [with_group(r, batting_stats(r), by) for r in db.query(sql, [*params, player_id])]


def batting_from_balls(db, matches_sql: str, params: list, player_id: str, by: str | None = None) -> list[dict]:
    """The definition of batting(): the same totals rebuilt from the ball rows of the one player. Kept for the export's check and for the tests, not used by a route."""
    head, group, order = grouping(by)
    sql = f"WITH m AS ({matches_sql}), {batting_innings(True, by)} SELECT {head} {BATTING_TOTALS} FROM played {group} {order}"
    return [with_group(r, batting_stats(r), by) for r in db.query(sql, [*params, *[player_id] * 5])]


def batting_all_from_balls(matches_sql: str) -> str:
    """Every player's batting totals over the selected matches as a SQL relation `totals`, built from the ball rows. This is the definition: the export uses it to write player_batting_innings and
    checks the table against it, and the tests compare the leaderboards with it. A request does not use it, see batting_all."""
    return f"WITH m AS ({matches_sql}), {batting_innings(False)}, totals AS (SELECT pid, {BATTING_TOTALS} FROM played GROUP BY pid)"


BATTING_INNINGS_ROWS = "SELECT pid AS player_id, match_id, innings, opponent, runs, balls, fours, sixes, is_out FROM played"
BOWLING_SPELLS_ROWS = "SELECT pid AS player_id, match_id, innings, balls, runs, wickets, wides, noballs, maidens FROM spells"


def batting_all(matches_sql: str) -> str:
    """Every player's batting totals over the selected matches as a SQL relation `totals` (pid and the BATTING_TOTALS columns), for the leaderboards. It reads the export's one row per player
    per innings (player_batting_innings) and joins the selected matches, which gives the same `played` rows as batting_all_from_balls, because a row's values do not depend on which matches are selected."""
    return f"WITH m AS ({matches_sql}), {batting_played_table()}, totals AS (SELECT pid, {BATTING_TOTALS} FROM played GROUP BY pid)"


def bowling_stats(row: dict) -> dict:
    balls, runs, wickets = int(row["balls"]), int(row["runs"]), int(row["wickets"])
    return {"matches": row["matches"], "innings": row["innings"], "balls": balls, "runs": runs, "wickets": wickets, "maidens": row["maidens"],
            "average": round(runs / wickets, 2) if wickets else None, "economy": rate(runs, balls, 6, 6), "strike_rate": round(balls / wickets, 2) if wickets else None,
            "best_wickets": row["best_wickets"], "best_runs": row["best_runs"], "four_wicket_hauls": row["four_wicket_hauls"], "five_wicket_hauls": row["five_wicket_hauls"],
            "wides": int(row["wides"]), "noballs": int(row["noballs"])}


BOWLING_TOTALS = """count(DISTINCT match_id) AS matches, count(*) AS innings, coalesce(sum(balls), 0) AS balls, coalesce(sum(runs), 0) AS runs, coalesce(sum(wickets), 0) AS wickets,
           coalesce(sum(maidens), 0) AS maidens, arg_max(wickets, {best}) AS best_wickets, arg_max(runs, {best}) AS best_runs,
           count(*) FILTER (WHERE wickets >= 4) AS four_wicket_hauls, count(*) FILTER (WHERE wickets >= 5) AS five_wicket_hauls, coalesce(sum(wides), 0) AS wides, coalesce(sum(noballs), 0) AS noballs""".format(best=BEST_FIGURES)


def bowling_spells(one_player: bool) -> str:
    """The WITH clauses that give one row per innings bowled for every player (`spells`, with `pid`), or for the one player whose id is passed three times. `m` must be defined."""
    where_mine = "b.bowler_id = ?" if one_player else "b.bowler_id IS NOT NULL"
    over_filter = " AND (b.match_id, b.innings, b.over) IN (SELECT match_id, innings, over FROM balls WHERE bowler_id = ?)" if one_player else ""
    # every delivery of an over is read, so that sole ownership and completeness are judged on the whole over; only the overs the player bowled in are read for one player
    maiden_overs = MAIDEN_OVERS.format(where="WHERE b.match_id IN (SELECT match_id FROM m)" + over_filter)
    maiden_pid = " WHERE bowler_id = ?" if one_player else ""
    return f"""
    reg AS (SELECT match_id, innings, batting_team FROM innings WHERE NOT is_super_over AND match_id IN (SELECT match_id FROM m)),
    mine AS (SELECT b.match_id, b.innings, b.bowler_id AS pid, m.season, m.start_date, m.venue, m.collection, m.format, r.batting_team AS opponent, coalesce(b.runs_off_bat, 0) AS bat, coalesce(b.wides, 0) AS wides, coalesce(b.noballs, 0) AS noballs,
                    b.is_legal_delivery AS legal, b.wicket_type IN ({CREDITED_SQL}) AS wicket
             FROM balls b JOIN m ON m.match_id = b.match_id JOIN reg r ON r.match_id = b.match_id AND r.innings = b.innings WHERE {where_mine}),
    maiden AS ({maiden_overs}),
    maidens AS (SELECT match_id, innings, bowler_id AS pid, count(*) AS n FROM maiden{maiden_pid} GROUP BY match_id, innings, bowler_id),
    spells AS (
        SELECT s.pid, s.match_id, s.innings, s.season, s.start_date, s.venue, s.collection, s.format, s.opponent, s.balls, s.runs, s.wickets, s.wides, s.noballs, coalesce(mc.n, 0) AS maidens
        FROM (SELECT pid, match_id, innings, any_value(season) AS season, any_value(start_date) AS start_date, any_value(venue) AS venue, any_value(collection) AS collection,
                     any_value(format) AS format, any_value(opponent) AS opponent, count(*) FILTER (WHERE legal) AS balls, sum(bat + wides + noballs) AS runs,
                     count(*) FILTER (WHERE wicket) AS wickets, sum(wides) AS wides, sum(noballs) AS noballs FROM mine GROUP BY pid, match_id, innings) s
        LEFT JOIN maidens mc ON mc.match_id = s.match_id AND mc.innings = s.innings AND mc.pid = s.pid)"""


def bowling_spells_table(by: str | None = None, one_player: bool = False) -> str:
    """The `spells` relation read from player_bowling_spells joined to the selected matches `m`. The opponent of a bowler is the batting team of the innings (the `innings` table, as in bowling_spells)."""
    opponent = "i.batting_team" if by == "opponent" else "CAST(NULL AS VARCHAR)"
    join = "JOIN innings i ON i.match_id = p.match_id AND i.innings = p.innings" if by == "opponent" else ""
    where = " WHERE p.player_id = ?" if one_player else ""
    return (f"spells AS (SELECT p.player_id AS pid, p.match_id, p.innings, m.season, m.start_date, m.venue, m.collection, m.format, {opponent} AS opponent, "
            f"p.balls, p.runs, p.wickets, p.wides, p.noballs, p.maidens FROM player_bowling_spells p JOIN m ON m.match_id = p.match_id {join}{where})")


def bowling(db, matches_sql: str, params: list, player_id: str, by: str | None = None) -> list[dict]:
    """A player's bowling totals, from the per-innings table. bowling_from_balls is the definition it must equal."""
    head, group, order = grouping(by)
    sql = f"WITH m AS ({matches_sql}), {bowling_spells_table(by, True)} SELECT {head} {BOWLING_TOTALS} FROM spells {group} {order}"
    return [with_group(r, bowling_stats(r), by) for r in db.query(sql, [*params, player_id])]


def bowling_from_balls(db, matches_sql: str, params: list, player_id: str, by: str | None = None) -> list[dict]:
    """The definition of bowling(): rebuilt from the ball rows of the one player. Not used by a route."""
    head, group, order = grouping(by)
    sql = f"WITH m AS ({matches_sql}), {bowling_spells(True)} SELECT {head} {BOWLING_TOTALS} FROM spells {group} {order}"
    return [with_group(r, bowling_stats(r), by) for r in db.query(sql, [*params, *[player_id] * 3])]


def bowling_all_from_balls(matches_sql: str) -> str:
    """The bowling counterpart of batting_all_from_balls: the definition, built from the ball rows."""
    return f"WITH m AS ({matches_sql}), {bowling_spells(False)}, totals AS (SELECT pid, {BOWLING_TOTALS} FROM spells GROUP BY pid)"


def bowling_all(matches_sql: str) -> str:
    """Every player's bowling totals over the selected matches as a SQL relation `totals` (pid and the BOWLING_TOTALS columns), for the leaderboards. It reads the export's player_bowling_spells
    and joins the selected matches, which gives the same `spells` rows as bowling_all_from_balls."""
    return f"WITH m AS ({matches_sql}), {bowling_spells_table()}, totals AS (SELECT pid, {BOWLING_TOTALS} FROM spells GROUP BY pid)"


def fielding_stats(row: dict) -> dict:
    return {"catches": row["caught"] + row["caught_and_bowled"], "catches_as_fielder": row["caught"], "caught_and_bowled": row["caught_and_bowled"], "stumpings": row["stumpings"],
            "run_out_involvements": row["run_outs"], "substitute_involvements": row["substitutes"]}


FIELDER_OPPONENT_JOIN = "JOIN innings i ON i.match_id = e.match_id AND i.innings = e.innings"


def fielding(db, matches_sql: str, params: list, player_id: str, by: str | None = None) -> list[dict]:
    """A player's fielding totals (career, or grouped by season or a split dimension), from the export's one row per wicket fielded (player_fielding_events). fielding_from_balls is the definition it must equal.
    The opponent is the batting side of the innings, as in the definition."""
    head, group, order = grouping(by)
    opponent = "i.batting_team" if by == "opponent" else "CAST(NULL AS VARCHAR)"
    join = FIELDER_OPPONENT_JOIN if by == "opponent" else ""
    sql = f"""
    WITH m AS ({matches_sql}),
    events AS (SELECT e.match_id, m.season, m.start_date, m.venue, m.collection, m.format, {opponent} AS opponent, e.kind
               FROM player_fielding_events e JOIN m ON m.match_id = e.match_id {join} WHERE e.player_id = ?)
    SELECT {head} count(DISTINCT match_id) AS matches, count(*) FILTER (WHERE kind = 'caught') AS caught, count(*) FILTER (WHERE kind = 'stumped') AS stumpings,
           count(*) FILTER (WHERE kind = 'run_out') AS run_outs, count(*) FILTER (WHERE kind = 'substitute') AS substitutes,
           count(*) FILTER (WHERE kind = 'caught_and_bowled') AS caught_and_bowled
    FROM events {group} {order}"""
    out = []
    for r in db.query(sql, [*params, player_id]):
        stats = fielding_stats(r)
        out.append(with_group(r, {"matches": r["matches"], **stats} if by in SPLIT_COLUMNS else stats, by))
    return out


def fielding_from_balls(db, matches_sql: str, params: list, player_id: str, by: str | None = None) -> list[dict]:
    """The definition of fielding(): rebuilt from the wicket, fielder and ball rows of the one player. Not used by a route."""
    head, group, order = grouping(by)
    sql = f"""
    WITH m AS ({matches_sql}),
    reg AS (SELECT match_id, innings, batting_team FROM innings WHERE NOT is_super_over AND match_id IN (SELECT match_id FROM m)),
    wk AS (SELECT w.match_id, w.innings, w.delivery_number, w.wicket_number, w.wicket_type, m.season, m.start_date, m.venue, m.collection, m.format, r.batting_team AS opponent
           FROM wickets w JOIN reg r ON r.match_id = w.match_id AND r.innings = w.innings JOIN m ON m.match_id = w.match_id),
    credited AS (SELECT f.match_id, any_value(wk.season) AS season, any_value(wk.start_date) AS start_date, any_value(wk.venue) AS venue, any_value(wk.collection) AS collection,
                        any_value(wk.format) AS format, any_value(wk.opponent) AS opponent,
                        CASE WHEN bool_and(coalesce(f.substitute, false)) THEN 'substitute' WHEN any_value(wk.wicket_type) = 'caught' THEN 'caught'
                             WHEN any_value(wk.wicket_type) = 'stumped' THEN 'stumped' WHEN any_value(wk.wicket_type) = 'run out' THEN 'run_out' END AS kind
                 FROM wicket_fielders f JOIN wk ON wk.match_id = f.match_id AND wk.innings = f.innings AND wk.delivery_number = f.delivery_number AND wk.wicket_number = f.wicket_number
                 WHERE f.fielder_id = ? GROUP BY f.match_id, f.innings, f.delivery_number, f.wicket_number),
    bowled AS (SELECT DISTINCT wk.match_id, wk.innings, wk.delivery_number, wk.wicket_number, wk.season, wk.start_date, wk.venue, wk.collection, wk.format, wk.opponent,
                      'caught_and_bowled' AS kind FROM wk
               JOIN balls b ON b.match_id = wk.match_id AND b.innings = wk.innings AND b.delivery_number = wk.delivery_number
               WHERE wk.wicket_type = 'caught and bowled' AND b.bowler_id = ?),
    events AS (SELECT match_id, season, start_date, venue, collection, format, opponent, kind FROM credited WHERE kind IS NOT NULL
               UNION ALL SELECT match_id, season, start_date, venue, collection, format, opponent, kind FROM bowled)
    SELECT {head} count(DISTINCT match_id) AS matches, count(*) FILTER (WHERE kind = 'caught') AS caught, count(*) FILTER (WHERE kind = 'stumped') AS stumpings,
           count(*) FILTER (WHERE kind = 'run_out') AS run_outs, count(*) FILTER (WHERE kind = 'substitute') AS substitutes,
           count(*) FILTER (WHERE kind = 'caught_and_bowled') AS caught_and_bowled
    FROM events {group} {order}"""
    out = []
    for r in db.query(sql, [*params, player_id, player_id]):
        stats = fielding_stats(r)
        out.append(with_group(r, {"matches": r["matches"], **stats} if by in SPLIT_COLUMNS else stats, by))
    return out


def matchup_stats(row: dict, kinds: list[dict]) -> dict:
    runs, balls, dismissals = int(row["runs"]), int(row["balls_faced"]), int(row["dismissals"])
    return {"matches": row["matches"], "innings": row["innings"], "deliveries": row["deliveries"], "balls_faced": balls, "runs": runs, "fours": int(row["fours"]), "sixes": int(row["sixes"]),
            "strike_rate": rate(runs, balls, 6, 100), "dismissals": dismissals, "bowler_wickets": int(row["bowler_wickets"]),
            "average": round(runs / dismissals, 2) if dismissals else None, "balls_per_dismissal": round(balls / dismissals, 2) if dismissals else None,
            "dismissal_kinds": kinds}


def matchup(db, matches_sql: str, params: list, batter_id: str, bowler_id: str) -> dict:
    """One batter against one bowler, from the export's one row per batter, bowler, match and innings (batter_bowler_innings). matchup_from_balls is the definition it must equal, and the rules are written
    there: balls faced exclude wides, a Super Over is left out, a dismissal is a wicket on this bowler's delivery in which the batter went out (retirements excepted) and includes a run out at the non-striker's end."""
    sql = f"""
    WITH m AS ({matches_sql}),
    pair AS (SELECT t.* FROM batter_bowler_innings t JOIN m ON m.match_id = t.match_id WHERE t.batter_id = ? AND t.bowler_id = ?),
    wickets_out AS (SELECT unnest(dismissal_kinds) AS kind FROM pair)"""
    row = db.query(sql + f""" SELECT count(DISTINCT match_id) AS matches, count(*) AS innings, coalesce(sum(deliveries), 0) AS deliveries, coalesce(sum(balls_faced), 0) AS balls_faced,
           coalesce(sum(runs), 0) AS runs, coalesce(sum(fours), 0) AS fours, coalesce(sum(sixes), 0) AS sixes, coalesce(sum(len(dismissal_kinds)), 0) AS dismissals,
           (SELECT count(*) FROM wickets_out WHERE kind IN ({CREDITED_SQL})) AS bowler_wickets FROM pair""", [*params, batter_id, bowler_id])[0]
    kinds = db.query(sql + " SELECT kind, count(*) AS count FROM wickets_out GROUP BY kind ORDER BY count(*) DESC, kind", [*params, batter_id, bowler_id])
    return matchup_stats(row, kinds)


def matchup_from_balls(db, matches_sql: str, params: list, batter_id: str, bowler_id: str) -> dict:
    """The definition of matchup(): one batter against one bowler, rebuilt from the ball and wicket rows. Not used by a route.

    Batting figures are of the balls the batter faced on strike from this bowler: a wide is a delivery but not a ball faced, and a no ball is both. Dismissals are every
    wicket on this bowler's deliveries in which the batter went out, retired hurt and retired not out excepted, and they include a run out at the non-striker's end, which
    the bowler did not cause. Bowler wickets are the dismissals of kinds credited to a bowler. Every canonical wicket row counts on its own, so a ball with two wickets gives
    each batter his dismissal. A Super Over is left out.
    """
    sql = f"""
    WITH m AS ({matches_sql}),
    reg AS (SELECT match_id, innings FROM innings WHERE NOT is_super_over AND match_id IN (SELECT match_id FROM m)),
    faced AS (SELECT b.match_id, b.innings, coalesce(b.runs_off_bat, 0) AS runs, coalesce(b.wides, 0) = 0 AS is_ball,
                     b.runs_off_bat = 4 AND NOT b.non_boundary AS is_four, b.runs_off_bat = 6 AND NOT b.non_boundary AS is_six
              FROM balls b JOIN reg r ON r.match_id = b.match_id AND r.innings = b.innings WHERE b.striker_id = ? AND b.bowler_id = ?),
    out AS (SELECT w.match_id, w.innings, w.wicket_type, w.wicket_type IN ({CREDITED_SQL}) AS credited
            FROM wickets w JOIN reg r ON r.match_id = w.match_id AND r.innings = w.innings
            JOIN balls b ON b.match_id = w.match_id AND b.innings = w.innings AND b.delivery_number = w.delivery_number
            WHERE w.player_dismissed_id = ? AND b.bowler_id = ? AND w.wicket_type NOT IN ({NOT_A_WICKET_SQL})),
    met AS (SELECT match_id, innings FROM faced UNION SELECT match_id, innings FROM out)
    SELECT (SELECT count(DISTINCT match_id) FROM met) AS matches, (SELECT count(*) FROM met) AS innings, (SELECT count(*) FROM faced) AS deliveries,
           (SELECT count(*) FILTER (WHERE is_ball) FROM faced) AS balls_faced, (SELECT coalesce(sum(runs), 0) FROM faced) AS runs,
           (SELECT count(*) FILTER (WHERE is_four) FROM faced) AS fours, (SELECT count(*) FILTER (WHERE is_six) FROM faced) AS sixes,
           (SELECT count(*) FROM out) AS dismissals, (SELECT count(*) FILTER (WHERE credited) FROM out) AS bowler_wickets"""
    args = [*params, batter_id, bowler_id, batter_id, bowler_id]
    row = db.query(sql, args)[0]
    kinds = db.query(sql.split("SELECT (SELECT count(DISTINCT")[0] + "SELECT wicket_type AS kind, count(*) AS count FROM out GROUP BY wicket_type ORDER BY count(*) DESC, wicket_type", args)
    return matchup_stats(row, kinds)


SPLIT_DIMENSIONS = {"opponent": "opponent", "venue": "venue", "competition": "collection", "format": "format"}
SPLIT_KINDS = {"batting": batting, "bowling": bowling, "fielding": fielding}


def split_label(dimension: str, key):
    """The readable name of a group. A competition is the Cricsheet collection code with its usual name beside it, and a format keeps its code."""
    if dimension == "competition":
        return COLLECTION_LABELS.get(key, key)
    if dimension == "format":
        return FORMAT_LABELS.get(key, key)
    return key


def split_order(row: dict) -> tuple:
    """Most matches first, then the key from A to Z, and a group with no key last. The same order every time, so a cursor can continue it."""
    return (-row["matches"], row["key"] is None, row["key"] or "")


def splits(db, kind: str, matches_sql: str, params: list, player_id: str, dimension: str) -> list[dict]:
    """A player's batting, bowling or fielding totals grouped by opponent, venue, competition or format, in split_order.

    The filters select the matches first and the groups are made from what is left. A group is listed only when the player has a record in it. The opponent is the side the player
    faced in that innings: the bowling side for a batter, and the batting side for a bowler or a fielder. It is read from the innings, not from the order of the two teams of the match.
    Every count adds up across the groups to the career total. Rates are made from each group's own sums, and a highest score or a best figure is the best of the group.
    """
    column = SPLIT_DIMENSIONS[dimension]            # a KeyError here means a dimension outside the allow-list
    rows = SPLIT_KINDS[kind](db, matches_sql, params, player_id, by=column)
    for row in rows:
        row["label"] = split_label(dimension, row["key"])
    return sorted(rows, key=split_order)
