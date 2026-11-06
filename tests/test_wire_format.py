"""The JSON the routes return must not change when the code behind them does.

Each response is compared with a saved copy: same keys, same order, same values. Values that change with every build
(the data version, the build time and the cursors that carry the data version) are masked. To save new copies after a
change that is meant to alter the output, run with UPDATE_SNAPSHOTS=1 and read the diff before committing.
"""
import json
import os
import re
from pathlib import Path

import pytest

SNAPSHOTS = Path(__file__).parent / "snapshots" / "wire.json"
VOLATILE = {"data_version", "data_built_utc", "next_cursor"}
REQUESTS = [
    "/health", "/v1/meta", "/v1/competitions", "/v1/competitions?collection=ipl", "/v1/teams", "/v1/teams?q=alp&collection=ipl&limit=1", "/v1/venues", "/v1/venues?q=ground",
    "/v1/players?q=one", "/v1/players?q=100%25", "/v1/matches", "/v1/matches?limit=1", "/v1/matches?gender=female", "/v1/matches?team=Alpha&date_from=2020-01-01",
    "/v1/matches/1", "/v1/matches/2", "/v1/matches/3", "/v1/matches/1/scorecard", "/v1/matches/3/scorecard",
    "/v1/matches/1/innings/1/balls", "/v1/matches/1/innings/1/balls?limit=2", "/v1/matches/1/innings/1/balls?fields=striker,runs_off_bat,wicket_type",
    "/v1/matches/1/innings/2/balls", "/v1/matches/3/innings/1/balls?limit=10",
    "/v1/matches/1/powerplays", "/v1/matches/1/reviews", "/v1/matches/1/replacements", "/v1/matches/1/wickets", "/v1/matches/1/notes", "/v1/matches/2/powerplays",
    "/v1/matches/2/reviews", "/v1/matches/2/replacements", "/v1/matches/2/wickets", "/v1/matches/2/notes",
    "/v1/players/a1", "/v1/players/f1", "/v1/players/a4", "/v1/players/a1/fielding", "/v1/players/f1/fielding", "/v1/players/b1/fielding?format=ODI",
    "/v1/leaderboards/batting?metric=runs", "/v1/leaderboards/batting?metric=average&min_balls=1&min_dismissals=1", "/v1/leaderboards/bowling?metric=wickets",
    "/v1/leaderboards/bowling?metric=economy&min_balls=1&limit=2", "/v1/leaderboards/bowling?metric=best_figures",
    "/v1/matches?q=alpha leeds", "/v1/matches?q=2021", "/v1/matches?q=zzz", "/v1/matches?q=alpha&limit=1", "/v1/matches?team=Alpha&opponent=Gamma", "/v1/matches?team=Beta&opponent=Delta",
    "/v1/matches?team=Alpha&opponent=Delta", "/v1/competitions?q=cup", "/v1/competitions?q=ipl", "/v1/competitions?q=zzz",
    "/v1/teams/record?team=Alpha&gender=male", "/v1/teams/record?team=Gamma&gender=female", "/v1/teams/record?team=Alpha&gender=male&date_from=2030-01-01",
    "/v1/teams/record/splits?team=Alpha&gender=male&by=format", "/v1/teams/record/splits?team=Alpha&gender=male&by=venue&limit=1", "/v1/head-to-head?team=Alpha&opponent=Beta&gender=male",
    "/v1/head-to-head?team=Beta&opponent=Alpha&gender=male", "/v1/head-to-head?team=Alpha&opponent=Delta&gender=male", "/v1/head-to-head/splits?team=Alpha&opponent=Beta&gender=male&by=season",
    "/v1/teams?gender=female", "/v1/teams?gender=male&q=alp",
    "/v1/players/a1/batting/splits?by=opponent", "/v1/players/a1/batting/splits?by=venue", "/v1/players/a1/batting/splits?by=competition", "/v1/players/a1/batting/splits?by=format",
    "/v1/players/b1/bowling/splits?by=opponent", "/v1/players/f1/fielding/splits?by=opponent", "/v1/players/a1/batting/splits?by=opponent&limit=1", "/v1/players/a1/batting/splits?by=format&format=ODI",
    "/v1/players/x1/batting/splits?by=venue",
    "/v1/players/a1/matches", "/v1/players/f1/matches", "/v1/players/a4/matches", "/v1/players/b5/matches", "/v1/players/x1/matches", "/v1/players/a1/matches?limit=1",
    "/v1/players/a1/matches?format=ODI",
    "/v1/matchups?batter=a1&bowler=b1", "/v1/matchups?batter=a3&bowler=b2", "/v1/matchups?batter=a2&bowler=b1&format=ODI", "/v1/matchups?batter=a1&bowler=a1",
    "/v1/players/a1/batting/seasons", "/v1/players/b1/bowling/seasons", "/v1/players/f1/fielding/seasons", "/v1/players/a1/batting/seasons?format=ODI",
    "/v1/players/a1/batting", "/v1/players/a2/batting", "/v1/players/b1/bowling", "/v1/players/a1/batting?format=ODI", "/v1/players/b1/bowling?collection=ipl&date_from=2020-01-01",
]


def volatile(key, value) -> bool:
    """A value that changes with every build. dataset_display_name is the hash itself when the data has no snapshot date, so only that fallback is masked: a real name such as "Data of 2026-10-28" is not."""
    if value is None:
        return False
    return key in VOLATILE or (key == "dataset_display_name" and isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value) is not None)


def mask(value):
    if isinstance(value, dict):
        return {k: ("<masked>" if volatile(k, v) else mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    return value


def current(client):
    out = {}
    for path in REQUESTS:
        response = client.get(path)
        out[path] = {"status": response.status_code, "content_type": response.headers["content-type"], "body": mask(json.loads(response.text))}
    return out


def test_responses_match_the_saved_copies(client):
    now = current(client)
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOTS.write_text(json.dumps(now, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        pytest.skip("snapshots written")
    saved = json.loads(SNAPSHOTS.read_text(encoding="utf-8"))
    assert list(saved) == list(now)
    for path in saved:
        # dumps without sort_keys, so a changed key order fails too
        assert json.dumps(now[path], ensure_ascii=False) == json.dumps(saved[path], ensure_ascii=False), path
