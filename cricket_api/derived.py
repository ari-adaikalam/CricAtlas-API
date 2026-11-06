"""Tables the export derives from the other tables so that a request reads a few rows instead of rebuilding them from the ball rows.

Each table is the definition the routes had before (the SQL in metrics.py and history.py, written for one player or one pair), written once for every player or pair at the same time. The rules are the
same rules: only the player or pair is no longer a parameter. A route keeps its old code as a `*_from_balls` function, and the export and the tests compare the two (export.check_derived_tables,
tests/test_derived_tables.py), so a table can never drift from the rule it stands for.

  player_fielding_events   one row per wicket a player fielded or took caught and bowled, with its kind (metrics.fielding)
  player_match_involvement one row per player and match with the indicators of history.py and the team they are credited with
  batter_bowler_innings    one row per batter, bowler, match and innings that met, with what the batter did against the bowler and the kinds of every wicket in which they went out to that bowler (metrics.matchup)
  match_scorecards         the scorecard of each match, as the text of the data the scorecard route returns (scorecard.build)

None of these holds a Super Over innings except player_match_involvement and match_scorecards: the others leave it out as the career rules do, and the involvement table keeps it as history does.
"""
from cricket_api.scorecard import CREDITED, NOT_A_WICKET

CREDITED_SQL = ", ".join(f"'{k}'" for k in sorted(CREDITED))
NOT_A_WICKET_SQL = ", ".join(f"'{k}'" for k in sorted(NOT_A_WICKET))

FIELDING_EVENTS = """
    WITH reg AS (SELECT match_id, innings FROM innings WHERE NOT is_super_over),
    wk AS (SELECT w.match_id, w.innings, w.delivery_number, w.wicket_number, w.wicket_type FROM wickets w JOIN reg r ON r.match_id = w.match_id AND r.innings = w.innings),
    credited AS (SELECT f.fielder_id AS player_id, f.match_id, f.innings, f.delivery_number, f.wicket_number,
                        CASE WHEN bool_and(coalesce(f.substitute, false)) THEN 'substitute' WHEN any_value(wk.wicket_type) = 'caught' THEN 'caught'
                             WHEN any_value(wk.wicket_type) = 'stumped' THEN 'stumped' WHEN any_value(wk.wicket_type) = 'run out' THEN 'run_out' END AS kind
                 FROM wicket_fielders f JOIN wk ON wk.match_id = f.match_id AND wk.innings = f.innings AND wk.delivery_number = f.delivery_number AND wk.wicket_number = f.wicket_number
                 WHERE f.fielder_id IS NOT NULL GROUP BY f.fielder_id, f.match_id, f.innings, f.delivery_number, f.wicket_number),
    bowled AS (SELECT DISTINCT b.bowler_id AS player_id, wk.match_id, wk.innings, wk.delivery_number, wk.wicket_number, 'caught_and_bowled' AS kind FROM wk
               JOIN balls b ON b.match_id = wk.match_id AND b.innings = wk.innings AND b.delivery_number = wk.delivery_number
               WHERE wk.wicket_type = 'caught and bowled' AND b.bowler_id IS NOT NULL)
    SELECT player_id, match_id, innings, delivery_number, wicket_number, kind FROM credited WHERE kind IS NOT NULL
    UNION ALL SELECT player_id, match_id, innings, delivery_number, wicket_number, kind FROM bowled"""

INVOLVEMENT = """
    WITH evidence AS (
        SELECT person_id AS pid, match_id, 'squad' AS kind, CAST(NULL AS SMALLINT) AS innings, team FROM match_people WHERE role = 'player' AND person_id IS NOT NULL
        UNION ALL SELECT striker_id, match_id, 'bat', innings, batting_team FROM balls WHERE striker_id IS NOT NULL
        UNION ALL SELECT p.person_id, b.match_id, 'bat', b.innings, b.batting_team FROM balls b JOIN match_people p ON p.match_id = b.match_id AND p.person = b.non_striker
                  AND p.team = b.batting_team AND p.role = 'player' WHERE p.person_id IS NOT NULL
        UNION ALL SELECT player_dismissed_id, match_id, 'bat', innings, CAST(NULL AS VARCHAR) FROM wickets WHERE player_dismissed_id IS NOT NULL
        UNION ALL SELECT bowler_id, match_id, 'bowl', innings, bowling_team FROM balls WHERE bowler_id IS NOT NULL
        UNION ALL SELECT f.fielder_id, f.match_id, CASE WHEN f.substitute THEN 'sub' ELSE 'field' END, f.innings, b.bowling_team FROM wicket_fielders f
                  LEFT JOIN balls b ON b.match_id = f.match_id AND b.innings = f.innings AND b.delivery_number = f.delivery_number WHERE f.fielder_id IS NOT NULL
        UNION ALL SELECT player_id, match_id, 'absent', innings, CAST(NULL AS VARCHAR) FROM innings_absent_hurt WHERE player_id IS NOT NULL
        UNION ALL SELECT player_in_id, match_id, 'in', innings, team FROM match_replacements WHERE player_in_id IS NOT NULL
        UNION ALL SELECT player_out_id, match_id, 'out', innings, team FROM match_replacements WHERE player_out_id IS NOT NULL),
    tagged AS (SELECT e.pid, e.match_id, e.kind, e.team, coalesce(i.is_super_over, false) AS is_super_over FROM evidence e
               JOIN matches x ON x.match_id = e.match_id LEFT JOIN innings i ON i.match_id = e.match_id AND i.innings = e.innings)
    SELECT pid AS player_id, match_id, bool_or(kind = 'squad') AS in_squad, bool_or(kind = 'bat') AS batted, bool_or(kind = 'bowl') AS bowled, bool_or(kind = 'field') AS fielded,
           bool_or(kind = 'sub') AS fielded_as_substitute, bool_or(kind = 'absent') AS absent_hurt, bool_or(kind = 'in') AS replaced_in, bool_or(kind = 'out') AS replaced_out,
           bool_or(is_super_over AND kind IN ('bat', 'bowl', 'field', 'sub')) AS super_over, bool_or(kind IN ('bat', 'bowl', 'field', 'sub')) AS recorded_on_field,
           coalesce(min(team) FILTER (WHERE kind = 'squad'), min(team) FILTER (WHERE kind IN ('in', 'out')), min(team) FILTER (WHERE kind IN ('bat', 'bowl')),
                    min(team) FILTER (WHERE kind IN ('field', 'sub'))) AS team
    FROM tagged GROUP BY pid, match_id"""

BATTER_BOWLER = f"""
    WITH reg AS (SELECT match_id, innings FROM innings WHERE NOT is_super_over),
    faced AS (SELECT b.striker_id AS batter_id, b.bowler_id, b.match_id, b.innings, count(*) AS deliveries, count(*) FILTER (WHERE coalesce(b.wides, 0) = 0) AS balls_faced,
                     sum(coalesce(b.runs_off_bat, 0)) AS runs, count(*) FILTER (WHERE b.runs_off_bat = 4 AND NOT b.non_boundary) AS fours,
                     count(*) FILTER (WHERE b.runs_off_bat = 6 AND NOT b.non_boundary) AS sixes
              FROM balls b JOIN reg r ON r.match_id = b.match_id AND r.innings = b.innings WHERE b.striker_id IS NOT NULL AND b.bowler_id IS NOT NULL
              GROUP BY b.striker_id, b.bowler_id, b.match_id, b.innings),
    out AS (SELECT w.player_dismissed_id AS batter_id, b.bowler_id, w.match_id, w.innings, list(w.wicket_type ORDER BY w.wicket_type, w.delivery_number, w.wicket_number) AS dismissal_kinds
            FROM wickets w JOIN reg r ON r.match_id = w.match_id AND r.innings = w.innings
            JOIN balls b ON b.match_id = w.match_id AND b.innings = w.innings AND b.delivery_number = w.delivery_number
            WHERE w.player_dismissed_id IS NOT NULL AND b.bowler_id IS NOT NULL AND w.wicket_type NOT IN ({NOT_A_WICKET_SQL})
            GROUP BY w.player_dismissed_id, b.bowler_id, w.match_id, w.innings)
    SELECT coalesce(f.batter_id, o.batter_id) AS batter_id, coalesce(f.bowler_id, o.bowler_id) AS bowler_id, coalesce(f.match_id, o.match_id) AS match_id, coalesce(f.innings, o.innings) AS innings,
           coalesce(f.deliveries, 0) AS deliveries, coalesce(f.balls_faced, 0) AS balls_faced, coalesce(f.runs, 0) AS runs, coalesce(f.fours, 0) AS fours, coalesce(f.sixes, 0) AS sixes,
           coalesce(o.dismissal_kinds, CAST([] AS VARCHAR[])) AS dismissal_kinds
    FROM faced f FULL OUTER JOIN out o ON o.batter_id = f.batter_id AND o.bowler_id = f.bowler_id AND o.match_id = f.match_id AND o.innings = f.innings"""
