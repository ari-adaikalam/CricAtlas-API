"""What a request costs in rate-limit tokens. The costs follow the measured CPU of each route (docs/measured_results.md): a leaderboard 5, a player's batting and bowling career, seasons and splits 1
(they read a small per-innings table), fielding the same, the match history and matchups 2, and the scorecard, ball pages and other heavy routes 3."""
import pytest

from cricket_api.app import cost_for


@pytest.mark.parametrize("path,cost", [
    ("/health", 0), ("/v1/leaderboards/batting", 5), ("/v1/leaderboards/bowling", 5),
    ("/v1/players/e087956b/batting", 1), ("/v1/players/e087956b/batting/seasons", 1), ("/v1/players/e087956b/batting/splits", 1),
    ("/v1/players/e087956b/bowling", 1), ("/v1/players/e087956b/bowling/seasons", 1), ("/v1/players/e087956b/bowling/splits", 1),
    ("/v1/players/x/batting", 1),
    ("/v1/players/e087956b/fielding", 1), ("/v1/players/e087956b/fielding/seasons", 1), ("/v1/players/e087956b/fielding/splits", 1),
    ("/v1/players/e087956b/matches", 2), ("/v1/matches/1152848/scorecard", 3), ("/v1/matches/1152848/innings/1/balls", 3), ("/v1/matchups", 2),
    ("/v1/teams/record", 3), ("/v1/head-to-head", 3),
    ("/v1/matches", 1), ("/v1/players", 1), ("/v1/players/e087956b", 1), ("/v1/competitions", 1), ("/v1/matches/1152848", 1),
])
def test_the_cost_of_each_kind_of_route(path, cost):
    assert cost_for(path) == cost


def test_a_player_page_fits_inside_the_burst():
    """A page for one player asks for batting and bowling career, seasons and splits (by opponent, venue, competition and format): 2 x (1 + 1 + 4) calls. It was 36 tokens against a burst of 20 and a refill of 60 a minute."""
    page = [f"/v1/players/p/{kind}{tail}" for kind in ("batting", "bowling") for tail in ("", "/seasons", "/splits", "/splits", "/splits", "/splits")]
    assert len(page) == 12 and sum(cost_for(p) for p in page) == 12 <= 20


def test_a_full_player_page_fits_the_burst_twice_over_and_match_detail_stays_at_one_token():
    """Detail, the three kinds of career with seasons, the history and the matchup: 1 + 3 + 3 + 3 (batting, bowling, fielding: career, seasons, splits once each is 9) + 2 + 2. Match detail is 1 by the owner's decision."""
    page = ["/v1/players/p", *[f"/v1/players/p/{k}{t}" for k in ("batting", "bowling", "fielding") for t in ("", "/seasons", "/splits")], "/v1/players/p/matches", "/v1/matchups"]
    assert sum(cost_for(p) for p in page) == 1 + 9 + 2 + 2 == 14 <= 20
    assert cost_for("/v1/matches/1152848") == 1
