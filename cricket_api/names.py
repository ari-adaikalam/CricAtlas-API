"""Public field names. Every JSON key is snake_case. The files keep their own names, so a few columns with spaces are renamed here."""

SPACED = {"striker balls faced": "striker_balls_faced", "total striker runs": "total_striker_runs", "player out runs": "player_out_runs",
          "player out balls faced": "player_out_balls_faced", "batting style_striker": "batting_style_striker",
          "bowling style_striker": "bowling_style_striker", "playing role_striker": "playing_role_striker",
          "batting style_bowler": "batting_style_bowler", "bowling style_bowler": "bowling_style_bowler", "playing role_bowler": "playing_role_bowler"}
FILE_NAME = {public: column for column, public in SPACED.items()}


def public(column: str) -> str:
    return SPACED.get(column, column)


def column(name: str) -> str:
    """The column a public name stands for. A name that is not a known public name is returned as it is."""
    return FILE_NAME.get(name, name)


def rename(row: dict) -> dict:
    return {public(key): value for key, value in row.items()}
