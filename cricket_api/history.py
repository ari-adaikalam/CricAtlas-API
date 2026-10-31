"""A player's match history: every match the data connects to the player, once, with what the records say about their part in it.

A match is listed when any record names the player in it. There are nine kinds of evidence, and each sets its own indicator, so a reader can tell a player who batted from
one who was only named:

  in_squad              a squad list of the match gives the player the role player
  batted                the player faced a ball, stood at the non-striker's end for one, or was dismissed
  bowled                the player bowled a delivery
  fielded               the player is a named fielder, not a substitute, on a wicket
  fielded_as_substitute the player is recorded as a substitute fielder on a wicket. The wicket data cannot establish every substitute appearance.
  absent_hurt           the match lists the player as absent hurt for an innings
  replaced_in           a replacement entry brings the player in
  replaced_out          a replacement entry takes the player out

In addition, super_over says a batting, bowling or fielding record falls in a Super Over innings, and recorded_on_field says the player batted, bowled or fielded (as
a named fielder or a substitute). Squad membership, an absent hurt entry and a replacement entry never set recorded_on_field, because none of them proves the player took
the field. Match history is a different question from statistics: a Super Over record counts here as involvement although the career totals leave Super Overs out.

team is the team the records put the player in: the squad list first, then a replacement entry, then the side they batted or bowled for, then the side they fielded for.
Matches are newest first, by start date and then match id, and each appears once however many records mention the player.
"""
from cricket_api import metrics

DETAILS = ("match_id", "start_date", "season", "event", "collection", "format", "gender", "venue", "city", "team_1", "team_2", "winner", "outcome")
INDICATORS = ("in_squad", "batted", "bowled", "fielded", "fielded_as_substitute", "absent_hurt", "replaced_in", "replaced_out", "super_over", "recorded_on_field")

EVIDENCE = """
    evidence AS (
        SELECT match_id, 'squad' AS kind, CAST(NULL AS SMALLINT) AS innings, team FROM match_people WHERE person_id = ? AND role = 'player'
        UNION ALL SELECT match_id, 'bat', innings, batting_team FROM balls WHERE striker_id = ?
        UNION ALL SELECT b.match_id, 'bat', b.innings, b.batting_team FROM balls b JOIN match_people p ON p.match_id = b.match_id AND p.person = b.non_striker
                  AND p.team = b.batting_team AND p.role = 'player' WHERE p.person_id = ?
        UNION ALL SELECT match_id, 'bat', innings, CAST(NULL AS VARCHAR) FROM wickets WHERE player_dismissed_id = ?
        UNION ALL SELECT match_id, 'bowl', innings, bowling_team FROM balls WHERE bowler_id = ?
        UNION ALL SELECT f.match_id, CASE WHEN f.substitute THEN 'sub' ELSE 'field' END, f.innings, b.bowling_team FROM wicket_fielders f
                  LEFT JOIN balls b ON b.match_id = f.match_id AND b.innings = f.innings AND b.delivery_number = f.delivery_number WHERE f.fielder_id = ?
        UNION ALL SELECT match_id, 'absent', innings, CAST(NULL AS VARCHAR) FROM innings_absent_hurt WHERE player_id = ?
        UNION ALL SELECT match_id, 'in', innings, team FROM match_replacements WHERE player_in_id = ?
        UNION ALL SELECT match_id, 'out', innings, team FROM match_replacements WHERE player_out_id = ?)"""
EVIDENCE_PARAMETERS = 9


def player_matches(db, matches_sql: str, params: list, player_id: str, after: tuple | None, limit: int) -> list[dict]:
    """The player's matches under the filters, newest first, from just after the keyset `after` (a start date and a match id), up to limit rows. Read from the export's one row per player and match
    (player_match_involvement), which holds the indicators and the team resolved from the same evidence as the definition (player_matches_from_balls). The filters choose matches and nothing else, so they
    are applied to the matches, as the definition does."""
    cursor = "AND (x.start_date, x.match_id) < (?, ?)" if after else ""
    sql = f"""
    WITH m AS ({matches_sql})
    SELECT {', '.join('x.' + c for c in DETAILS)}, h.team, {', '.join('h.' + c for c in INDICATORS)}
    FROM player_match_involvement h JOIN matches x ON x.match_id = h.match_id JOIN m ON m.match_id = h.match_id WHERE h.player_id = ? {cursor}
    ORDER BY x.start_date DESC, x.match_id DESC LIMIT ?"""
    return db.query(sql, [*params, player_id, *(after or ()), limit])


def player_matches_from_balls(db, matches_sql: str, params: list, player_id: str, after: tuple | None, limit: int) -> list[dict]:
    """The definition of player_matches(): the same rows rebuilt from the ball, wicket, squad and replacement records of the one player. Not used by a route. The player's matches under the filters, newest first, from just after the keyset `after` (a start date and a match id), up to limit rows."""
    cursor = "WHERE (m.start_date, m.match_id) < (?, ?)" if after else ""
    sql = f"""
    WITH m AS ({matches_sql}),{EVIDENCE},
    tagged AS (SELECT e.match_id, e.kind, e.team, coalesce(i.is_super_over, false) AS is_super_over FROM evidence e
               JOIN m ON m.match_id = e.match_id LEFT JOIN innings i ON i.match_id = e.match_id AND i.innings = e.innings),
    history AS (
        SELECT match_id, bool_or(kind = 'squad') AS in_squad, bool_or(kind = 'bat') AS batted, bool_or(kind = 'bowl') AS bowled, bool_or(kind = 'field') AS fielded,
               bool_or(kind = 'sub') AS fielded_as_substitute, bool_or(kind = 'absent') AS absent_hurt, bool_or(kind = 'in') AS replaced_in, bool_or(kind = 'out') AS replaced_out,
               bool_or(is_super_over AND kind IN ('bat', 'bowl', 'field', 'sub')) AS super_over, bool_or(kind IN ('bat', 'bowl', 'field', 'sub')) AS recorded_on_field,
               coalesce(min(team) FILTER (WHERE kind = 'squad'), min(team) FILTER (WHERE kind IN ('in', 'out')), min(team) FILTER (WHERE kind IN ('bat', 'bowl')),
                        min(team) FILTER (WHERE kind IN ('field', 'sub'))) AS team
        FROM tagged GROUP BY match_id)
    SELECT {', '.join('x.' + c for c in DETAILS)}, h.team, {', '.join('h.' + c for c in INDICATORS)}
    FROM history h JOIN matches x ON x.match_id = h.match_id JOIN m ON m.match_id = h.match_id {cursor.replace('m.', 'x.')}
    ORDER BY x.start_date DESC, x.match_id DESC LIMIT ?"""
    args = [*params, *[player_id] * EVIDENCE_PARAMETERS, *(after or ()), limit]
    return db.query(sql, args)
