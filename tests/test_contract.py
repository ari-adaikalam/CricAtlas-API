import pytest

from cricket_api import contract


def test_the_contract_is_clean():
    assert contract.problems() == []


@pytest.mark.parametrize("name", ["ball_line", "shot_played", "wagon_x", "win_probability", "country", "image url",
                                  "line_confidence", "Cricinfo_id", "predicted_score", "some_espn_field", "wagon_anything"])
def test_banned_names_are_caught(name):
    assert contract.banned(name)


@pytest.mark.parametrize("name", ["match_id", "striker", "runs_off_bat", "is_legal_delivery", "striker balls faced", "event", "type", "format",
                                  "venue", "toss_winner", "player_of_match_2", "over", "ball_in_over", "country_striker", "batting style_bowler",
                                  "striker_age", "bowler_age"])
def test_allowed_columns_are_not_banned(name):
    assert not contract.banned(name)


def test_a_banned_column_in_a_table_is_a_problem(monkeypatch):
    monkeypatch.setitem(contract.PLAYERS, "country", "VARCHAR")
    assert any("players.country is a banned name" in p for p in contract.problems())
    monkeypatch.setitem(contract.BALLS, "ball_line", "VARCHAR")
    assert any("balls.ball_line is a banned name" in p for p in contract.problems())


def test_a_key_that_is_not_an_allowed_column_is_a_problem(monkeypatch):
    monkeypatch.setitem(contract.TABLES["matches"], "key", ["nope"])
    assert any("nope" in p for p in contract.problems())


def test_every_link_names_allowed_columns():
    for table, column, parent, parent_column in contract.LINKS:
        assert column in contract.TABLES[table]["columns"] and parent_column in contract.TABLES[parent]["columns"]
