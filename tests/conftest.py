"""A tiny dataset, written through the real export, and a server over it."""
import csv
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cricket_api import contract, export
from cricket_api.app import create_app
from cricket_api.settings import Settings

TEST_SECRET = "tests-only-signing-secret-0123456789abcdef"       # the settings refuse a signing secret shorter than 32 characters, so the fixtures use a long one


def write_table(folder, name, rows):
    spec = contract.TABLES[name]
    if spec["source"] is None:          # a derived table is built by the export, there is no CSV for it
        return
    columns = list(spec["columns"])
    with open(folder / spec["source"], "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c, "") for c in columns])


def ball(match, innings, number, **extra):
    base = {"match_id": match, "season": "2020", "start_date": "2020-03-01", "innings": innings, "delivery_number": number, "over": (number - 1) // 6,
            "ball_in_over": (number - 1) % 6 + 1, "scorecard_ball": f"{(number - 1) // 6}.{(number - 1) % 6 + 1}", "is_legal_delivery": "True", "batting_team": "Alpha",
            "bowling_team": "Beta", "striker": "A One", "striker_id": "a1", "non_striker": "A Two", "bowler": "B One", "bowler_id": "b1", "runs_off_bat": 0, "extras": 0,
            "wicket": 0, "non_boundary": "False", "gender": "male", "event": "Cup", "format": "T20", "type": "Intl"}
    base.update(extra)
    return base


MATCHES = [
    {"match_id": 1, "season": "2020", "start_date": "2020-03-01", "gender": "male", "event": "Cup", "format": "T20", "type": "Fran(IPL)", "venue": "Ground One, Leeds",
     "team_1": "Alpha", "team_2": "Beta", "city": "Leeds", "end_date": "2020-03-01", "scheduled_overs": 20, "balls_per_over": 6, "collection": "ipl", "winner": "Alpha",
     "winner_runs": 5, "won_by_innings": "False", "cricsheet_created": "2020-04-01", "cricsheet_revision": 2, "cricsheet_data_version": "1.1.0"},
    {"match_id": 2, "season": "2021", "start_date": "2021-05-10", "gender": "female", "event": "Series", "format": "ODI", "type": "Intl", "venue": "Ground Two",
     "team_1": "Gamma", "team_2": "Alpha", "end_date": "2021-05-10", "scheduled_overs": 50, "balls_per_over": 6, "collection": "odis", "winner": "Gamma", "won_by_innings": "False"},
    {"match_id": 3, "season": "2022", "start_date": "2022-01-05", "gender": "male", "event": "Hundred", "format": "T20", "type": "Fran(Hundred)", "venue": "Ground Three",
     "team_1": "Delta", "team_2": "Beta", "end_date": "2022-01-05", "balls_per_over": 5, "collection": "hnd", "won_by_innings": "False", "outcome": "no result"},
]
# match 1, innings 1: Alpha bat. A One scores 4, then A Two is bowled (B One), then A Three is caught by F One off B Two. Innings 2: Beta bat, 3 balls.
BALLS = [
    ball(1, 1, 1, runs_off_bat=4, extras=0), ball(1, 1, 2, wides=1, extras=1, is_legal_delivery="False"),
    ball(1, 1, 3, striker="A Two", striker_id="a2", non_striker="A One", non_striker_id="a1", wicket=1, wicket_type="bowled", player_dismissed="A Two"),
    ball(1, 1, 4, striker="A Three", striker_id="a3", non_striker="A One", runs_off_bat=6, bowler="B Two", bowler_id="b2"),
    ball(1, 1, 5, striker="A Three", striker_id="a3", non_striker="A One", wicket=1, wicket_type="caught", player_dismissed="A Three", fielder_1="F One", bowler="B Two", bowler_id="b2"),
    ball(1, 1, 6, striker="A One", striker_id="a1", non_striker="A Four", legbyes=1, extras=1, runs_off_bat=0),
    ball(1, 2, 1, batting_team="Beta", bowling_team="Alpha", striker="B Three", striker_id="b3", non_striker="B Four", bowler="A Five", bowler_id="a5", runs_off_bat=1),
    ball(1, 2, 2, batting_team="Beta", bowling_team="Alpha", striker="B Four", striker_id="b4", non_striker="B Three", bowler="A Five", bowler_id="a5", wicket=1,
         wicket_type="retired hurt", player_dismissed="B Four"),
    ball(2, 1, 1, start_date="2021-05-10", season="2021", runs_off_bat=2, batting_team="Gamma", bowling_team="Alpha", gender="female", format="ODI"),
    # match 3 is a five-ball-over match: five dots from one bowler make a maiden, and a sixth ball in the next over does not
    *[ball(3, 1, n, start_date="2022-01-05", season="2022", batting_team="Delta", bowling_team="Beta", striker="D One", striker_id="d1", bowler="B One", bowler_id="b1",
           over=0, ball_in_over=n, scorecard_ball=f"0.{n}") for n in range(1, 6)],
    ball(3, 1, 6, start_date="2022-01-05", season="2022", batting_team="Delta", bowling_team="Beta", striker="D One", striker_id="d1", bowler="B One", bowler_id="b1",
         over=1, ball_in_over=1, scorecard_ball="1.1"),
]
WICKETS = [
    {"match_id": 1, "innings": 1, "delivery_number": 3, "wicket_number": 1, "player_dismissed": "A Two", "player_dismissed_id": "a2", "wicket_type": "bowled"},
    {"match_id": 1, "innings": 1, "delivery_number": 5, "wicket_number": 1, "player_dismissed": "A Three", "player_dismissed_id": "a3", "wicket_type": "caught"},
    {"match_id": 1, "innings": 2, "delivery_number": 2, "wicket_number": 1, "player_dismissed": "B Four", "player_dismissed_id": "b4", "wicket_type": "retired hurt"},
]
INNINGS = [
    {"match_id": 1, "innings": 1, "batting_team": "Alpha", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 5, "penalty_runs_post": 0},
    {"match_id": 1, "innings": 2, "batting_team": "Beta", "target_runs": 20, "target_overs": 20, "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0},
    {"match_id": 2, "innings": 1, "batting_team": "Gamma", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0},
    {"match_id": 3, "innings": 1, "batting_team": "Delta", "is_super_over": "False", "declared": "False", "penalty_runs_pre": 0, "penalty_runs_post": 0},
]
WICKET_FIELDERS = [{"match_id": 1, "innings": 1, "delivery_number": 5, "wicket_number": 1, "fielder_number": 1, "fielder": "F One", "fielder_id": "f1", "substitute": "False"}]
PEOPLE = [
    {"match_id": 1, "role": "player", "person_number": 1, "team": "Alpha", "person": "A One", "person_id": "a1"},
    {"match_id": 1, "role": "player", "person_number": 2, "team": "Alpha", "person": "A Two", "person_id": "a2"},
    {"match_id": 1, "role": "player", "person_number": 3, "team": "Beta", "person": "B One", "person_id": "b1"},
    {"match_id": 1, "role": "umpire", "person_number": 1, "person": "U One", "person_id": "u1"},
]
PLAYERS = [{"player_id": i, "name": n} for i, n in (("a1", "A One"), ("a2", "A Two"), ("a3", "A Three"), ("a5", "A Five"), ("b1", "B One"), ("b2", "B Two"), ("b3", "B Three"),
                                                    ("b4", "B Four"), ("d1", "D One"), ("x1", "100% Real_Name"))]


# one of each of the parts that sit beside a scorecard, all on match 1; matches 2 and 3 have none, so their routes answer with empty lists
POWERPLAYS = [{"match_id": 1, "innings": 1, "powerplay_number": 1, "type": "mandatory", "start_over": 0, "start_ball_in_over": 1, "start_delivery_number": 1,
               "end_over": 0, "end_ball_in_over": 6, "end_delivery_number": 6, "start_listed": "0.1", "end_listed": "0.6"}]
REVIEWS = [{"match_id": 1, "innings": 1, "delivery_number": 3, "review_by": "Beta", "umpire": "U One", "umpire_id": "u1", "batter": "A Two", "batter_id": "a2",
            "decision": "upheld", "review_type": "wicket", "umpires_call": "False"}]
REPLACEMENTS = [{"match_id": 1, "innings": 1, "delivery_number": 4, "replacement_number": 1, "kind": "role", "team": "Alpha", "role": "batter", "player_in": "A Four",
                 "player_in_id": "a4", "player_out": "A Three", "player_out_id": "a3", "reason": "injury"}]
MISSING = [{"match_id": 1, "missing_number": 1, "missing_type": "powerplays", "innings": 2, "powerplay_type": "fielding"},
           {"match_id": 1, "missing_number": 2, "missing_type": "player_of_match"}]
MISCOUNTED = [{"match_id": 1, "innings": 1, "over": 0, "balls": 5, "umpire": "U One", "umpire_id": "u1"}]
ABSENT_HURT = [{"match_id": 1, "innings": 2, "player": "B Four", "player_id": "b4"}, {"match_id": 1, "innings": 2, "player": "B Five", "player_id": "b5"}]
DATES = [{"match_id": 1, "date_number": 1, "date": "2020-03-01"}, {"match_id": 1, "date_number": 2, "date": "2020-03-02"}]


@pytest.fixture(autouse=True)
def fresh_fault_throttle():
    """The fault log is limited for the whole process, so one test's faults must not count against the next test's."""
    from cricket_api import faults
    faults.THROTTLE.reset()
    yield
    faults.THROTTLE.reset()


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    root = tmp_path_factory.mktemp("serve")
    source = root / "output"
    source.mkdir()
    rows = {"balls": BALLS, "matches": MATCHES, "players": PLAYERS, "wickets": WICKETS, "innings": INNINGS, "match_people": PEOPLE, "wicket_fielders": WICKET_FIELDERS,
            "powerplays": POWERPLAYS, "reviews": REVIEWS, "match_replacements": REPLACEMENTS, "match_missing": MISSING, "miscounted_overs": MISCOUNTED,
            "innings_absent_hurt": ABSENT_HURT, "match_dates": DATES}
    for name in contract.TABLES:
        write_table(source, name, rows.get(name, []))
    meta, problems = export.build(source, root / "api")
    assert problems == [], problems
    return root / "api"


@pytest.fixture
def make_client(dataset):
    clients = []

    def build(**overrides):
        settings = Settings(**{"data_dir": dataset, "cursor_secret": TEST_SECRET, "rate_per_minute": 6000, "rate_burst": 1000, **overrides})
        client = TestClient(create_app(settings), base_url="http://testserver")
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.app.state.db.close()


@pytest.fixture
def client(make_client):
    return make_client()


# The real matches the golden tests use: a small committed cut-out, and the full data when asked for (see test_golden.py).
HERE = Path(__file__).parent
FULL = HERE.parent / "data" / "exports" / "current"      # api/data/exports/current, a link to the latest export (the refresh sets it)
SOURCES = [pytest.param(HERE / "golden" / "api", id="cut-out")]
if os.environ.get("CRICKET_API_FULL") and FULL.exists():
    SOURCES.append(pytest.param(FULL, id="full-data"))


@pytest.fixture(scope="module", params=SOURCES)
def api(request):
    client = TestClient(create_app(Settings(data_dir=request.param, cursor_secret=TEST_SECRET, rate_per_minute=60000, rate_burst=10000)), base_url="http://testserver")
    yield client
    client.app.state.db.close()
