"""Every route has to ship with the whole set of safeguards, or say in writing why one does not apply.

For each route in the OpenAPI document this checks, without anyone having to remember:

  model        a strict Pydantic response model (extra fields rejected) named in the document
  openapi      a named 200 schema, and the problem details errors for a route that takes input
  snapshot     a saved copy of its response in test_wire_format.py
  leak         a good request and, for a route that takes input, a bad or injection request in test_no_leaks.py
  docs         a row in api/docs/reference.md
  golden       a test on real matches                 }
  independent  a test that recomputes the answer      }  named below, or exempt with a reason
  recorded     a test against a recorded result       }

A new route fails this test until all of that exists. The three named kinds are looked up by test name, so a renamed or deleted test fails too.
"""
import ast
import re
from pathlib import Path

import pytest

from cricket_api import models
from test_no_leaks import BAD, INJECTION, PATHS
from test_wire_format import REQUESTS

HERE = Path(__file__).parent
KINDS = ("golden", "independent", "recorded")
G, L, P, M, U, S, B, X, H, T, R = ("test_golden.py", "test_lists_golden.py", "test_player_routes.py", "test_match_parts.py", "test_player_universe.py", "test_seasons.py",
                                 "test_leaderboards.py", "test_matchups.py", "test_player_history.py", "test_splits.py", "test_teams.py")

NAMED = {
    "/freshness": {"golden": "test_freshness.py::test_coverage_on_real_matches_matches_the_match_list",
                   "independent": "test_freshness.py::test_coverage_on_real_matches_matches_the_match_list"},
    "/v1/competitions": {"golden": f"{L}::test_competitions_are_the_matches_counted_by_group", "independent": f"{L}::test_competitions_are_the_matches_counted_by_group"},
    "/v1/teams": {"golden": f"{L}::test_teams_and_venues_are_the_matches_counted_by_name", "independent": f"{L}::test_teams_and_venues_are_the_matches_counted_by_name"},
    "/v1/venues": {"golden": f"{L}::test_teams_and_venues_are_the_matches_counted_by_name", "independent": f"{L}::test_teams_and_venues_are_the_matches_counted_by_name"},
    "/v1/players": {"golden": f"{L}::test_player_search_finds_exactly_the_players_on_the_scorecards", "independent": f"{L}::test_player_search_finds_exactly_the_players_on_the_scorecards"},
    "/v1/matches": {"golden": f"{L}::test_paging_with_a_small_limit_returns_every_match_once_newest_first", "independent": f"{L}::test_filters_select_what_a_plain_filter_selects",
                    "recorded": f"{L}::test_a_match_row_agrees_with_the_recorded_result_rules"},
    "/v1/matches/{match_id}": {"golden": f"{G}::test_a_won_by_an_innings_match_has_only_three_innings", "independent": f"{G}::test_the_match_route_innings_agree_with_an_independent_count",
                               "recorded": f"{G}::test_a_duckworth_lewis_chase_carries_its_target"},
    "/v1/matches/{match_id}/scorecard": {"golden": f"{G}::test_headingley_2019_matches_the_published_scorecard", "independent": f"{G}::test_every_innings_agrees_with_an_independent_count",
                                         "recorded": f"{G}::test_the_innings_reproduce_the_recorded_result"},
    "/v1/matches/{match_id}/innings/{innings}/balls": {"golden": f"{G}::test_the_balls_of_every_innings_are_complete_and_in_order",
                                                      "independent": f"{G}::test_the_balls_of_every_innings_are_complete_and_in_order",
                                                      "recorded": f"{G}::test_the_match_route_innings_agree_with_an_independent_count"},
    "/v1/matches/{match_id}/powerplays": {"golden": f"{M}::test_the_2019_final_has_the_three_powerplays_of_the_time_in_each_innings", "independent": f"{M}::test_a_resolved_boundary_points_at_the_delivery_it_names",
                                          "recorded": f"{M}::test_a_twenty_over_match_has_one_powerplay_of_six_overs"},
    "/v1/matches/{match_id}/reviews": {"golden": f"{M}::test_the_2019_headingley_test_had_seventeen_reviews_and_the_final_six", "independent": f"{M}::test_every_review_sits_on_a_real_delivery_by_a_team_in_the_match_in_order",
                                       "recorded": f"{M}::test_no_side_loses_more_reviews_than_the_format_allows_in_an_innings"},
    "/v1/matches/{match_id}/replacements": {"golden": f"{M}::test_the_2023_qualifier_had_two_impact_players_a_concussion_substitute_and_an_injury_replacement",
                                            "independent": f"{M}::test_every_replacement_sits_on_a_real_delivery_and_changes_a_player",
                                            "recorded": f"{M}::test_the_2023_qualifier_had_two_impact_players_a_concussion_substitute_and_an_injury_replacement"},
    "/v1/matches/{match_id}/wickets": {"golden": f"{M}::test_ten_players_retired_out_on_one_ball_are_ten_wickets_of_one_delivery", "independent": f"{M}::test_the_wickets_are_exactly_the_dismissals_the_ball_rows_record",
                                       "recorded": f"{M}::test_wickets_reproduce_the_published_dismissals_and_the_scorecards"},
    "/v1/matches/{match_id}/notes": {"golden": f"{M}::test_the_dates_are_the_published_days_and_agree_with_the_match", "independent": f"{M}::test_the_miscounted_overs_say_how_many_balls_were_really_bowled",
                                     "recorded": f"{M}::test_absent_hurt_players_are_in_the_squad_and_missing_data_agrees_with_the_powerplays"},
    "/v1/players/{player_id}": {"golden": f"{U}::test_a_substitute_only_fielder_has_one_recorded_substitute_involvement_and_nothing_else",
                                "independent": f"{U}::test_every_id_the_matches_name_as_a_player_is_a_known_player"},
    "/v1/players/{player_id}/fielding": {"golden": f"{U}::test_every_named_fielder_of_a_run_out_gets_one_involvement",
                                         "independent": f"{U}::test_every_players_fielding_equals_the_count_from_the_wickets_route",
                                         "recorded": f"{U}::test_published_catches_of_headingley_2019_are_the_catches_of_the_fielders"},
    "/v1/players/{player_id}/batting/seasons": {"golden": f"{S}::test_stokes_2019_season_holds_his_published_135_not_out",
                                                "independent": f"{S}::test_every_players_seasons_equal_the_per_match_scorecard_lines_grouped_by_season",
                                                "recorded": f"{S}::test_stokes_2019_season_holds_his_published_135_not_out"},
    "/v1/players/{player_id}/bowling/seasons": {"golden": f"{S}::test_every_players_seasons_equal_the_per_match_scorecard_lines_grouped_by_season",
                                                "independent": f"{S}::test_every_players_seasons_equal_the_per_match_scorecard_lines_grouped_by_season",
                                                "recorded": f"{S}::test_a_seasons_bowlers_took_the_published_37_wickets_of_headingley_2019"},
    "/v1/players/{player_id}/fielding/seasons": {"golden": f"{S}::test_fielding_seasons_equal_the_plain_python_count_by_player_and_season",
                                                 "independent": f"{S}::test_fielding_seasons_equal_the_plain_python_count_by_player_and_season",
                                                 "recorded": f"{S}::test_season_catches_and_stumpings_equal_the_scorecard_dismissals"},
    "/v1/players/{player_id}/matches": {"golden": f"{H}::test_the_substitute_only_fielder_of_a_real_catch_is_listed_for_that_match", "independent": f"{H}::test_every_players_history_equals_the_plain_python_history",
                                        "recorded": f"{H}::test_stokes_in_the_2019_final_and_at_headingley_with_the_super_over_flagged"},
    "/v1/players/{player_id}/batting/splits": {"golden": f"{T}::test_stokes_at_headingley_2019_against_australia_and_in_the_final_against_new_zealand",
                                               "independent": f"{T}::test_every_players_groups_equal_the_plain_python_groups_in_every_count_and_rate",
                                               "recorded": f"{T}::test_stokes_at_headingley_2019_against_australia_and_in_the_final_against_new_zealand"},
    "/v1/players/{player_id}/bowling/splits": {"golden": f"{T}::test_ajaz_patel_at_mumbai_in_2021_took_14_wickets_for_225_and_ten_for_119_in_an_innings",
                                               "independent": f"{T}::test_every_players_groups_equal_the_plain_python_groups_in_every_count_and_rate",
                                               "recorded": f"{T}::test_ajaz_patel_at_mumbai_in_2021_took_14_wickets_for_225_and_ten_for_119_in_an_innings"},
    "/v1/players/{player_id}/fielding/splits": {"golden": f"{T}::test_the_2019_final_fielding_credits_follow_the_published_scorecard",
                                                "independent": f"{T}::test_every_players_groups_equal_the_plain_python_groups_in_every_count_and_rate",
                                                "recorded": f"{T}::test_the_2019_final_fielding_credits_follow_the_published_scorecard"},
    "/v1/teams/record": {"golden": f"{R}::test_published_test_results_headingley_antigua_and_mumbai", "independent": f"{R}::test_every_teams_record_equals_the_plain_python_recount_and_dates",
                         "recorded": f"{R}::test_the_2019_world_cup_final_is_a_recorded_tie_with_england_the_eliminator_winner"},
    "/v1/teams/record/splits": {"golden": f"{R}::test_published_results_come_through_the_splits_too", "independent": f"{R}::test_every_teams_splits_equal_the_recount_and_add_up_to_the_record",
                                "recorded": f"{R}::test_published_results_come_through_the_splits_too"},
    "/v1/head-to-head": {"golden": f"{R}::test_published_test_results_headingley_antigua_and_mumbai", "independent": f"{R}::test_every_pairing_equals_the_recount_and_its_reverse_is_the_mirror",
                         "recorded": f"{R}::test_the_2019_world_cup_final_is_a_recorded_tie_with_england_the_eliminator_winner"},
    "/v1/head-to-head/splits": {"golden": f"{R}::test_published_results_come_through_the_splits_too", "independent": f"{R}::test_every_pairings_splits_equal_the_recount_in_both_directions_over_pages",
                                "recorded": f"{R}::test_published_results_come_through_the_splits_too"},
    "/v1/matchups": {"golden": f"{X}::test_every_special_pair_matches_the_plain_python_count", "independent": f"{X}::test_the_most_met_pairs_and_every_dismissal_pair_match_the_plain_python_count",
                     "recorded": f"{X}::test_stokes_at_headingley_2019_scored_143_off_the_bowlers_and_was_dismissed_once"},
    "/v1/leaderboards/batting": {"golden": f"{B}::test_the_highest_score_is_lara_400_not_out", "independent": f"{B}::test_the_route_ranks_exactly_as_a_plain_python_ranking_of_the_career_routes",
                                 "recorded": f"{B}::test_headingley_2019_highest_score_is_stokes_135_not_out"},
    "/v1/leaderboards/bowling": {"golden": f"{B}::test_ajaz_patel_has_the_best_figures_ten_wickets_for_119", "independent": f"{B}::test_the_route_ranks_exactly_as_a_plain_python_ranking_of_the_career_routes",
                                 "recorded": f"{B}::test_ajaz_patel_has_the_best_figures_ten_wickets_for_119"},
    "/v1/players/{player_id}/batting": {"golden": f"{P}::test_stokes_career_in_the_two_matches_he_played_here", "independent": f"{P}::test_every_career_in_the_real_matches_equals_the_sum_of_its_scorecard_lines",
                                        "recorded": f"{P}::test_one_matchs_careers_add_up_to_its_published_totals"},
    "/v1/players/{player_id}/bowling": {"golden": f"{P}::test_stokes_career_in_the_two_matches_he_played_here", "independent": f"{P}::test_every_career_in_the_real_matches_equals_the_sum_of_its_scorecard_lines",
                                        "recorded": f"{P}::test_one_matchs_careers_add_up_to_its_published_totals"},
}
EXEMPT = {
    ("/freshness", "snapshot"): "source-check times change without a data rebuild; fixed coverage and no-op changes are checked in test_freshness.py",
    ("/freshness", "recorded"): "operational timestamps and dataset coverage, not a recorded cricket result",
    ("/health", "golden"): "operational: it says the process is up, with no cricket in it",
    ("/health", "independent"): "operational: nothing to recompute",
    ("/health", "recorded"): "operational: no recorded result",
    ("/status", "golden"): "operational: data age and request counters",
    ("/status", "independent"): "operational: counters of this process",
    ("/status", "recorded"): "operational: no recorded result",
    ("/status", "snapshot"): "request counters and times change on every call, so there is nothing stable to save",
    ("/v1/meta", "golden"): "describes the data files and the licence, with no cricket in it",
    ("/v1/meta", "independent"): "row counts are checked against the match list in test_lists_golden.py and against the files by the export",
    ("/v1/meta", "recorded"): "no recorded result",
    ("/v1/competitions", "recorded"): "counts of matches, with no result to compare",
    ("/v1/teams", "recorded"): "counts of matches, with no result to compare",
    ("/v1/venues", "recorded"): "counts of matches, with no result to compare",
    ("/v1/players", "recorded"): "a name search, with no result to compare",
    ("/v1/players/{player_id}", "recorded"): "an identity record with no result to compare: its names, ids and roles are checked against the match routes in the independent test",
}


def template_regex(template: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/?]+", re.escape(template)) + "$")


def covered(template: str, requests: list[str]) -> bool:
    pattern = template_regex(template)
    return any(pattern.match(r.split("?")[0]) for r in requests)


def functions(file_name: str) -> set[str]:
    tree = ast.parse((HERE / file_name).read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def normal(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path)


@pytest.fixture
def routes(client):
    return client.get("/openapi.json").json()


def inventory_problems(routes) -> list[str]:
    readme = normal((HERE.parent / "docs" / "reference.md").read_text(encoding="utf-8"))
    missing = []
    for template, item in routes["paths"].items():
        operation = item["get"]
        takes_input = bool(operation.get("parameters"))
        ref = operation["responses"]["200"]["content"]["application/json"]["schema"].get("$ref", "")
        model = getattr(models, ref.rsplit("/", 1)[-1], None)
        if model is None or model.model_config.get("extra") != "forbid":
            missing.append(f"{template}: no strict response model")
        if takes_input and not {"400", "429", "500"} <= set(operation["responses"]):
            missing.append(f"{template}: errors not documented")
        if ("snapshot" not in {k for r, k in EXEMPT if r == template}) and not covered(template, REQUESTS):
            missing.append(f"{template}: no saved response in test_wire_format.py")
        if not covered(template, PATHS):
            missing.append(f"{template}: no good request in test_no_leaks.py")
        if takes_input and not covered(template, BAD + INJECTION):
            missing.append(f"{template}: no bad or injection request in test_no_leaks.py")
        if normal(template.replace("{id}", "{}")) not in readme and not any(normal(template) in line for line in readme.splitlines()):
            missing.append(f"{template}: not in the README")
        for kind in KINDS:
            named, exempt = NAMED.get(template, {}).get(kind), EXEMPT.get((template, kind))
            if bool(named) == bool(exempt):
                missing.append(f"{template}: {kind} must be named or exempt, exactly one")
            elif named:
                file_name, test_name = named.split("::")
                if test_name not in functions(file_name):
                    missing.append(f"{template}: {kind} names {named}, which does not exist")
            elif not exempt.strip():
                missing.append(f"{template}: the {kind} exemption has no reason")
    return missing


def test_every_route_has_every_safeguard(routes):
    assert not inventory_problems(routes), "\n".join(inventory_problems(routes))


def test_a_new_route_without_its_safeguards_fails(routes):
    bare = {"get": {"parameters": [{"name": "q"}], "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Anything"}}}}}}}
    problems = inventory_problems({**routes, "paths": {**routes["paths"], "/v1/new/{thing}": bare}})
    wanted = ("no strict response model", "errors not documented", "no saved response", "no good request", "no bad or injection request", "not in the README",
              "golden must be named or exempt", "independent must be named or exempt", "recorded must be named or exempt")
    for text in wanted:
        assert any("/v1/new/{thing}" in p and text in p for p in problems), text


def test_the_inventory_names_no_route_that_does_not_exist(routes):
    known = set(routes["paths"])
    assert set(NAMED) <= known, set(NAMED) - known
    assert {r for r, _ in EXEMPT} <= known, {r for r, _ in EXEMPT} - known
    assert {k for _, k in EXEMPT} <= set(KINDS) | {"snapshot"}
