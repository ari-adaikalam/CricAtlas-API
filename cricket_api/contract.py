"""What the API data may contain.

The API serves what the published ball file holds: the Cricsheet columns and the player facts on each ball (country,
styles, role and age). It does not serve the separate tracking and prediction files, the player profile file beyond an
id and a name, or any profile id and image address. This file is the one place that says which columns that is. The
export writes only these columns, so the Parquet files cannot hold anything outside the list, and the API code reads
only the Parquet files.

A column is added here on purpose. A column that is not listed never reaches the API, whatever the source files hold.
"""

# DuckDB type for every allowed column, in the order the Parquet files keep them.
BALLS = {
    "match_id": "INTEGER", "season": "VARCHAR", "start_date": "DATE", "innings": "SMALLINT", "delivery_number": "INTEGER",
    "over": "SMALLINT", "ball_in_over": "SMALLINT", "scorecard_ball": "DOUBLE", "is_legal_delivery": "BOOLEAN",
    "batting_team": "VARCHAR", "bowling_team": "VARCHAR",
    "striker": "VARCHAR", "striker_id": "VARCHAR", "non_striker": "VARCHAR", "bowler": "VARCHAR", "bowler_id": "VARCHAR",
    "runs_off_bat": "SMALLINT", "extras": "SMALLINT", "wides": "SMALLINT", "noballs": "SMALLINT", "byes": "SMALLINT",
    "legbyes": "SMALLINT", "penalty": "SMALLINT",
    "wicket_type": "VARCHAR", "player_dismissed": "VARCHAR", "other_wicket_type": "VARCHAR", "other_player_dismissed": "VARCHAR",
    "wicket": "SMALLINT",
    "striker balls faced": "SMALLINT", "total striker runs": "SMALLINT", "player out runs": "SMALLINT", "player out balls faced": "SMALLINT",
    "gender": "VARCHAR", "event": "VARCHAR", "format": "VARCHAR", "type": "VARCHAR",
    "country_striker": "VARCHAR", "batting style_striker": "VARCHAR", "bowling style_striker": "VARCHAR", "playing role_striker": "VARCHAR",
    "striker_age": "SMALLINT",
    "country_bowler": "VARCHAR", "batting style_bowler": "VARCHAR", "bowling style_bowler": "VARCHAR", "playing role_bowler": "VARCHAR",
    "bowler_age": "SMALLINT",
    "fielder_1": "VARCHAR", "fielder_1_id": "VARCHAR", "fielder_1_substitute": "BOOLEAN", "non_boundary": "BOOLEAN",
}
MATCHES = {
    "match_id": "INTEGER", "season": "VARCHAR", "start_date": "DATE", "gender": "VARCHAR", "event": "VARCHAR", "format": "VARCHAR",
    "type": "VARCHAR", "venue": "VARCHAR", "match_number": "INTEGER", "toss_winner": "VARCHAR", "toss_decision": "VARCHAR",
    "player_of_match": "VARCHAR", "player_of_match_2": "VARCHAR", "winner": "VARCHAR", "winner_runs": "INTEGER",
    "winner_wickets": "INTEGER", "outcome": "VARCHAR",
    "team_1": "VARCHAR", "team_2": "VARCHAR", "city": "VARCHAR", "end_date": "DATE", "scheduled_overs": "SMALLINT", "balls_per_over": "SMALLINT",
    "match_type_number": "INTEGER", "collection": "VARCHAR", "result_method": "VARCHAR", "eliminator": "VARCHAR",
    "umpire_1": "VARCHAR", "umpire_2": "VARCHAR", "tv_umpire": "VARCHAR", "match_referee": "VARCHAR",
    "won_by_innings": "BOOLEAN", "cricsheet_created": "DATE", "cricsheet_revision": "INTEGER", "cricsheet_data_version": "VARCHAR",
    "event_group": "VARCHAR", "event_sub_name": "VARCHAR", "event_stage": "VARCHAR",
}
PLAYERS = {"player_id": "VARCHAR", "name": "VARCHAR"}
# Every spelling known for a player, with how many source rows use it. `players` holds the one chosen spelling.
PLAYER_NAMES = {"player_id": "VARCHAR", "name": "VARCHAR", "uses": "INTEGER"}
DELIVERY = {"match_id": "INTEGER", "innings": "SMALLINT", "delivery_number": "INTEGER"}
WICKETS = {**DELIVERY, "wicket_number": "SMALLINT", "player_dismissed": "VARCHAR", "player_dismissed_id": "VARCHAR", "wicket_type": "VARCHAR"}
WICKET_FIELDERS = {**DELIVERY, "wicket_number": "SMALLINT", "fielder_number": "SMALLINT", "fielder": "VARCHAR", "fielder_id": "VARCHAR", "substitute": "BOOLEAN"}
MATCH_PEOPLE = {"match_id": "INTEGER", "role": "VARCHAR", "person_number": "SMALLINT", "team": "VARCHAR", "person": "VARCHAR", "person_id": "VARCHAR"}
INNINGS = {"match_id": "INTEGER", "innings": "SMALLINT", "batting_team": "VARCHAR", "target_runs": "INTEGER", "target_overs": "DOUBLE", "is_super_over": "BOOLEAN",
           "declared": "BOOLEAN", "penalty_runs_pre": "SMALLINT", "penalty_runs_post": "SMALLINT"}
POWERPLAYS = {"match_id": "INTEGER", "innings": "SMALLINT", "powerplay_number": "SMALLINT", "type": "VARCHAR", "start_over": "SMALLINT", "start_ball_in_over": "SMALLINT",
               "start_delivery_number": "INTEGER", "end_over": "SMALLINT", "end_ball_in_over": "SMALLINT", "end_delivery_number": "INTEGER", "start_listed": "VARCHAR", "end_listed": "VARCHAR"}
REVIEWS = {**DELIVERY, "review_by": "VARCHAR", "umpire": "VARCHAR", "umpire_id": "VARCHAR", "batter": "VARCHAR", "batter_id": "VARCHAR", "decision": "VARCHAR",
           "review_type": "VARCHAR", "umpires_call": "BOOLEAN"}
MATCH_MISSING = {"match_id": "INTEGER", "missing_number": "SMALLINT", "missing_type": "VARCHAR", "innings": "SMALLINT", "powerplay_type": "VARCHAR"}
MISCOUNTED_OVERS = {"match_id": "INTEGER", "innings": "SMALLINT", "over": "SMALLINT", "balls": "SMALLINT", "umpire": "VARCHAR", "umpire_id": "VARCHAR"}
INNINGS_ABSENT_HURT = {"match_id": "INTEGER", "innings": "SMALLINT", "player": "VARCHAR", "player_id": "VARCHAR"}
MATCH_DATES = {"match_id": "INTEGER", "date_number": "SMALLINT", "date": "DATE"}
PLAYER_EXTERNAL_IDS = {"player_id": "VARCHAR", "source": "VARCHAR", "external_id": "VARCHAR"}
MATCH_REPLACEMENTS = {**DELIVERY, "replacement_number": "SMALLINT", "kind": "VARCHAR", "team": "VARCHAR", "role": "VARCHAR", "player_in": "VARCHAR", "player_in_id": "VARCHAR",
                      "player_out": "VARCHAR", "player_out_id": "VARCHAR", "reason": "VARCHAR"}

# Derived by the export from the other tables: one row per player per innings batted, and one per player per innings bowled, exactly as the career and leaderboard rules count them (metrics.py).
# The leaderboards read these instead of rebuilding them from every ball on every request. Super Overs are not in them.
PLAYER_BATTING_INNINGS = {"player_id": "VARCHAR", "match_id": "INTEGER", "innings": "SMALLINT", "opponent": "VARCHAR", "runs": "INTEGER", "balls": "INTEGER", "fours": "INTEGER", "sixes": "INTEGER", "is_out": "BOOLEAN"}
PLAYER_BOWLING_SPELLS = {"player_id": "VARCHAR", "match_id": "INTEGER", "innings": "SMALLINT", "balls": "INTEGER", "runs": "INTEGER", "wickets": "INTEGER", "wides": "INTEGER",
                         "noballs": "INTEGER", "maidens": "INTEGER"}
# Derived by the export, like the two above (derived.py holds the definitions): fielding events, a player's involvement in each match, what a batter did against a bowler in an innings, and each match's scorecard.
PLAYER_FIELDING_EVENTS = {"player_id": "VARCHAR", "match_id": "INTEGER", "innings": "SMALLINT", "delivery_number": "INTEGER", "wicket_number": "SMALLINT", "kind": "VARCHAR"}
PLAYER_MATCH_INVOLVEMENT = {"player_id": "VARCHAR", "match_id": "INTEGER", "in_squad": "BOOLEAN", "batted": "BOOLEAN", "bowled": "BOOLEAN", "fielded": "BOOLEAN", "fielded_as_substitute": "BOOLEAN",
                            "absent_hurt": "BOOLEAN", "replaced_in": "BOOLEAN", "replaced_out": "BOOLEAN", "super_over": "BOOLEAN", "recorded_on_field": "BOOLEAN", "team": "VARCHAR"}
BATTER_BOWLER_INNINGS = {"batter_id": "VARCHAR", "bowler_id": "VARCHAR", "match_id": "INTEGER", "innings": "SMALLINT", "deliveries": "INTEGER", "balls_faced": "INTEGER", "runs": "INTEGER",
                         "fours": "INTEGER", "sixes": "INTEGER", "dismissal_kinds": "VARCHAR[]"}
MATCH_SCORECARDS = {"match_id": "INTEGER", "scorecard": "VARCHAR"}
# The derived tables written from the ball rows by one SELECT each (derived.py), in the order they are written (after the tables they read).
INNINGS_TABLES = ("player_batting_innings", "player_bowling_spells")
DERIVED_SQL_TABLES = ("player_fielding_events", "player_match_involvement", "batter_bowler_innings")
DERIVED_TABLES = INNINGS_TABLES + DERIVED_SQL_TABLES + ("match_scorecards",)

# Columns that exist only when player profiles were fetched. The dataset leaves them out entirely when profiles are off, and the API follows.
PROFILE_BALL = [f"{name}_{role}" for role in ("striker", "bowler") for name in ("country", "batting style", "bowling style", "playing role")] + ["striker_age", "bowler_age"]

# source: the CSV the table is read from. key: unique per row. order: how the rows are sorted in the Parquet file, so a
# query for one match reads a few blocks and skips the rest.
TABLES = {
    "balls": {"source": "ball_by_ball_data.csv", "columns": BALLS, "key": ["match_id", "innings", "delivery_number"],
              "order": ["match_id", "innings", "delivery_number"]},
    "matches": {"source": "matches.csv", "columns": MATCHES, "key": ["match_id"], "order": ["match_id"]},
    # derived: the export builds it (see player_universe.py). players.csv is the first source of names, and the table also holds squad members, fielders,
    # dismissed players, absent hurt players and replacement players, so a known player is never a 404.
    "players": {"source": "players.csv", "columns": PLAYERS, "key": ["player_id"], "order": ["player_id"], "derived": True},
    "player_names": {"source": None, "columns": PLAYER_NAMES, "key": ["player_id", "name"], "order": ["player_id", "name"], "derived": True},
    "wickets": {"source": "wickets.csv", "columns": WICKETS, "key": list(DELIVERY) + ["wicket_number"], "order": list(DELIVERY) + ["wicket_number"]},
    "wicket_fielders": {"source": "wicket_fielders.csv", "columns": WICKET_FIELDERS, "key": list(DELIVERY) + ["wicket_number", "fielder_number"],
                        "order": list(DELIVERY) + ["wicket_number", "fielder_number"]},
    "match_people": {"source": "match_people.csv", "columns": MATCH_PEOPLE, "key": ["match_id", "role", "person_number"], "order": ["match_id", "role", "person_number"]},
    "innings": {"source": "innings.csv", "columns": INNINGS, "key": ["match_id", "innings"], "order": ["match_id", "innings"]},
    "powerplays": {"source": "powerplays.csv", "columns": POWERPLAYS, "key": ["match_id", "innings", "powerplay_number"], "order": ["match_id", "innings", "powerplay_number"]},
    "reviews": {"source": "reviews.csv", "columns": REVIEWS, "key": list(DELIVERY), "order": list(DELIVERY)},
    "match_replacements": {"source": "match_replacements.csv", "columns": MATCH_REPLACEMENTS, "key": list(DELIVERY) + ["replacement_number"],
                           "order": list(DELIVERY) + ["replacement_number"]},
    "match_missing": {"source": "match_missing.csv", "columns": MATCH_MISSING, "key": ["match_id", "missing_number"],
                      "order": ["match_id", "missing_number"]},
    "miscounted_overs": {"source": "miscounted_overs.csv", "columns": MISCOUNTED_OVERS, "key": ["match_id", "innings", "over"], "order": ["match_id", "innings", "over"]},
    "innings_absent_hurt": {"source": "innings_absent_hurt.csv", "columns": INNINGS_ABSENT_HURT, "key": ["match_id", "innings", "player"],
                            "order": ["match_id", "innings", "player"]},
    "match_dates": {"source": "match_dates.csv", "columns": MATCH_DATES, "key": ["match_id", "date_number"], "order": ["match_id", "date_number"]},
    "player_external_ids": {"source": "player_external_ids.csv", "columns": PLAYER_EXTERNAL_IDS, "key": ["player_id", "source", "external_id"],
                            "order": ["player_id", "source", "external_id"]},
    # derived from balls, matches, innings, wickets and match_people, so they are written last
    "player_batting_innings": {"source": None, "columns": PLAYER_BATTING_INNINGS, "key": ["player_id", "match_id", "innings"], "order": ["match_id", "innings", "player_id"], "derived": True},
    "player_bowling_spells": {"source": None, "columns": PLAYER_BOWLING_SPELLS, "key": ["player_id", "match_id", "innings"], "order": ["match_id", "innings", "player_id"], "derived": True},
    "player_fielding_events": {"source": None, "columns": PLAYER_FIELDING_EVENTS, "key": ["player_id", "match_id", "innings", "delivery_number", "wicket_number"],
                               "order": ["player_id", "match_id", "innings", "delivery_number", "wicket_number"], "derived": True},
    "player_match_involvement": {"source": None, "columns": PLAYER_MATCH_INVOLVEMENT, "key": ["player_id", "match_id"], "order": ["player_id", "match_id"], "derived": True},
    "batter_bowler_innings": {"source": None, "columns": BATTER_BOWLER_INNINGS, "key": ["batter_id", "bowler_id", "match_id", "innings"], "order": ["batter_id", "bowler_id", "match_id", "innings"], "derived": True},
    "match_scorecards": {"source": None, "columns": MATCH_SCORECARDS, "key": ["match_id"], "order": ["match_id"], "derived": True},
}

# Links between tables. Each pair is (table, column, parent table, parent column).
LINKS = [
    ("balls", "match_id", "matches", "match_id"),
    ("balls", "striker_id", "players", "player_id"),
    ("balls", "bowler_id", "players", "player_id"),
    ("player_names", "player_id", "players", "player_id"),
    ("wicket_fielders", "fielder_id", "players", "player_id"),
    ("wickets", "player_dismissed_id", "players", "player_id"),
    ("innings_absent_hurt", "player_id", "players", "player_id"),
    ("match_replacements", "player_in_id", "players", "player_id"),
    ("match_replacements", "player_out_id", "players", "player_id"),
    ("reviews", "batter_id", "players", "player_id"),
    ("wickets", "match_id", "matches", "match_id"),
    ("wicket_fielders", "match_id", "matches", "match_id"),
    ("match_people", "match_id", "matches", "match_id"),
    ("innings", "match_id", "matches", "match_id"),
    ("powerplays", "match_id", "matches", "match_id"),
    ("reviews", "match_id", "matches", "match_id"),
    ("match_replacements", "match_id", "matches", "match_id"),
    ("match_missing", "match_id", "matches", "match_id"),
    ("miscounted_overs", "match_id", "matches", "match_id"),
    ("innings_absent_hurt", "match_id", "matches", "match_id"),
    ("match_dates", "match_id", "matches", "match_id"),
    ("player_batting_innings", "player_id", "players", "player_id"),
    ("player_batting_innings", "match_id", "matches", "match_id"),
    ("player_bowling_spells", "player_id", "players", "player_id"),
    ("player_bowling_spells", "match_id", "matches", "match_id"),
    ("player_fielding_events", "player_id", "players", "player_id"),
    ("player_fielding_events", "match_id", "matches", "match_id"),
    ("player_match_involvement", "player_id", "players", "player_id"),
    ("player_match_involvement", "match_id", "matches", "match_id"),
    ("batter_bowler_innings", "batter_id", "players", "player_id"),
    ("batter_bowler_innings", "bowler_id", "players", "player_id"),
    ("batter_bowler_innings", "match_id", "matches", "match_id"),
    ("match_scorecards", "match_id", "matches", "match_id"),
]

# Names that must never appear in an API table, in a response or in an example: how a ball was bowled and played, the
# wagon wheel, win probability, anything predicted, and the profile ids, image addresses and career details that sit in
# the player file. The check is on the column name and on its text.
BANNED_NAMES = {
    "ball_line", "ball_length", "shot_played", "shot_control", "shot_direction", "wagon_x", "wagon_y", "wagon_zone",
    "wagon_angle", "wagon_distance", "wagon_in_ring", "ball_timestamp", "win_probability", "predicted_score", "ball_events",
    "line", "length", "shot", "direction", "line_confidence", "length_confidence", "shot_confidence", "direction_confidence",
    "line_explicit", "length_explicit", "shot_explicit", "direction_explicit",
    "country", "batting style", "bowling style", "playing role", "fielding style", "full name", "image url", "major teams",
    "first international year", "last international year", "cricinfo_id",
}
BANNED_WORDS = ("cricinfo", "espn", "wagon", "confidence", "predict")


def csv_tables() -> dict:
    """The tables that have a CSV file of their own. A derived table such as player_names has none."""
    return {name: spec for name, spec in TABLES.items() if spec["source"]}


def effective_tables(ball_header: list[str]) -> tuple[dict, dict]:
    """The tables for this build and its features. The profile columns are in the ball file together or not at all.

    Returns (tables, features). A ball file with only some of the profile columns is a fault, reported by the export.
    """
    present = [c for c in PROFILE_BALL if c in ball_header]
    profiles = len(present) == len(PROFILE_BALL)
    tables = {name: dict(spec) for name, spec in TABLES.items()}
    if not present:
        tables["balls"]["columns"] = {c: t for c, t in BALLS.items() if c not in PROFILE_BALL}
    return tables, {"player_profiles": profiles, "partial_profiles": bool(present) and not profiles}


def banned(name: str) -> bool:
    lowered = name.lower()
    return lowered in BANNED_NAMES or any(word in lowered for word in BANNED_WORDS)


def problems() -> list[str]:
    """Faults in the contract itself. An empty list means every allowed column passes the boundary."""
    found = []
    for table, spec in TABLES.items():
        for column in spec["columns"]:
            if banned(column):
                found.append(f"{table}.{column} is a banned name")
        for column in spec["key"] + spec["order"]:
            if column not in spec["columns"]:
                found.append(f"{table} uses {column} as a key or sort column, but it is not an allowed column")
    for table, column, parent, parent_column in LINKS:
        if column not in TABLES[table]["columns"] or parent_column not in TABLES[parent]["columns"]:
            found.append(f"link {table}.{column} to {parent}.{parent_column} names a column that is not allowed")
    return found
