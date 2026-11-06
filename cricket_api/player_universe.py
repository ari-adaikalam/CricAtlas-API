"""Who counts as a player, and which spelling of a name stands for them.

A player is anyone Cricsheet gives an id as one of: a batter or bowler on a ball, a squad member with the role player, a named fielder, a dismissed player, an
absent hurt player, or a replacement coming in or going out. People known only as officials are not players, and an id that appears only in the register of
external ids does not make a player either, because that file is not read here at all.

An id can be spelled more than one way (a name change, or a trailing "(2)" that Cricsheet's CSV adds to tell two people apart). The chosen name comes from the
first source that has the id, in this order: players.csv, then the names on the ball rows, then the squad lists, then everything else. Only that source's rows
are counted: the most used spelling in it wins and a tie goes to the name that sorts first, so a spelling common in a lower source never outvotes one in a higher
source, and the choice never depends on file order. Every spelling stays in player_names.
"""

SOURCES = """
SELECT player_id AS id, name, 0 AS rank FROM {players}
UNION ALL SELECT striker_id, striker, 1 FROM {balls}
UNION ALL SELECT bowler_id, bowler, 1 FROM {balls}
UNION ALL SELECT person_id, person, 2 FROM {match_people} WHERE role = 'player'
UNION ALL SELECT fielder_id, fielder, 3 FROM {wicket_fielders}
UNION ALL SELECT player_dismissed_id, player_dismissed, 3 FROM {wickets}
UNION ALL SELECT player_id, player, 3 FROM {innings_absent_hurt}
UNION ALL SELECT player_in_id, player_in, 3 FROM {match_replacements}
UNION ALL SELECT player_out_id, player_out, 3 FROM {match_replacements}
"""

NAMES_SQL = "SELECT id AS player_id, name, count(*) AS uses FROM ({sources}) WHERE id IS NOT NULL AND id <> '' AND name IS NOT NULL AND name <> '' GROUP BY id, name"

# The chosen name: find the first source in the order that has the id, count spellings only among that source's rows, and take the most used, then the one that sorts first.
# A spelling that is common in a lower source never outvotes a spelling in a higher one. Source 3 groups the smaller tables (fielders, dismissals and so on).
PLAYERS_SQL = """
WITH s AS (SELECT id, name, rank FROM ({sources}) WHERE id IS NOT NULL AND id <> '' AND name IS NOT NULL AND name <> ''),
best AS (SELECT id, min(rank) AS rank FROM s GROUP BY id),
counted AS (SELECT s.id, s.name, count(*) AS n FROM s JOIN best ON best.id = s.id AND best.rank = s.rank GROUP BY s.id, s.name)
SELECT player_id, name FROM (SELECT id AS player_id, name, row_number() OVER (PARTITION BY id ORDER BY n DESC, name) AS pick FROM counted) WHERE pick = 1
"""

# People who appear in a match only as an official. The check in the export asserts that none of them is in the player table.
OFFICIALS_ONLY_SQL = """
SELECT DISTINCT person_id AS id FROM {match_people} WHERE role <> 'player' AND person_id IS NOT NULL AND person_id <> ''
EXCEPT SELECT DISTINCT id FROM ({sources}) WHERE id IS NOT NULL AND id <> ''
"""


def sources_sql(relations: dict) -> str:
    """relations maps each CSV's short name to the SQL that reads it."""
    return SOURCES.format(**relations)


def conflicting(spellings: list[str]) -> bool:
    """Two spellings conflict when they differ after a trailing "(2)" style marker is removed."""
    import re
    return len({re.sub(r"\s*\(\d+\)$", "", s) for s in spellings}) > 1
