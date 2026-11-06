"""Keyword search of matches and competitions, and the opponent filter.

Every search is checked against an independent Python filter over the whole match list: lower case the keywords and the fields and keep a match when every keyword is part
of one of the fields. The cases include ones that are ambiguous, ones that find nothing, wildcard characters, and the bounds. A request without q or opponent must give what
it always gave, which the saved responses also pin.
"""
import pytest

from cricket_api import search
from test_golden import HEADINGLEY

FIELDS = ("team_1", "team_2", "event", "collection", "venue", "city", "season")
COMPETITION_FIELDS = ("event", "collection")


def every_match(api, **params):
    rows, cursor = [], None
    while True:
        page = api.get("/v1/matches", params={"limit": 500, **params, **({"cursor": cursor} if cursor else {})}).json()
        rows += page["data"]
        cursor = page["next_cursor"]
        if not cursor:
            return rows


def plain(rows, text, fields=FIELDS):
    """The rows in which every keyword is a piece of some field, ignoring case: the definition of the search, written without the route's SQL."""
    words = []
    for word in text.split():
        if word.lower() not in words:
            words.append(word.lower())
    return [r for r in rows if all(any(word in (r[f] or "").lower() for f in fields) for word in words)]


def is_full(api):
    """True with the full data, False with the small cut-out of real matches."""
    from test_golden import ALL
    return api.get("/v1/meta").json()["data"]["tables"]["matches"]["rows"] > len(ALL)


def ids(rows):
    return [r["match_id"] for r in rows]


@pytest.fixture(scope="module")
def everything(api):
    return every_match(api)


QUERIES = ["England Australia Headingley 2019", "headingley", "HEADINGLEY", "leeds", "ipl", "IPL 2019", "hundred", "2019", "india", "world cup", "australia england", "england england",
           "  england   australia  ", "test", "odis", "male", "zzzzqqq", "england zzzzqqq", "Caribbean", "women", "final"]


@pytest.mark.parametrize("text", QUERIES)
def test_a_search_gives_exactly_the_matches_a_plain_filter_gives_in_the_same_order(api, everything, text):
    got = every_match(api, q=text)
    assert ids(got) == ids(plain(everything, text)), text


def test_the_headingley_example_finds_the_headingley_test(api, everything):
    got = every_match(api, q="England Australia Headingley 2019")
    assert HEADINGLEY in ids(got)
    assert all({"England", "Australia"} <= {m["team_1"], m["team_2"]} and "headingley" in (m["venue"] or "").lower() and "2019" in m["season"] for m in got)


def test_the_order_and_the_case_of_the_keywords_do_not_matter(api):
    a = ids(every_match(api, q="England Australia 2019"))
    assert a == ids(every_match(api, q="2019 AUSTRALIA england")) == ids(every_match(api, q="england   australia 2019 england"))
    assert a, "there are England against Australia matches in 2019"


def test_an_ambiguous_keyword_finds_every_field_it_is_part_of(api, everything):
    """2019 is a piece of the season 2019 and of the season 2019/20, and a collection code finds the competition whose name does not contain it."""
    found = {m["season"] for m in every_match(api, q="2019")}
    assert found == {m["season"] for m in everything if "2019" in m["season"]}
    assert "2019" in found
    if is_full(api):
        assert {"2019", "2019/20"} <= found
    ipl = every_match(api, q="ipl")
    assert {m["collection"] for m in ipl} == {"ipl"} and len(ipl) == len([m for m in everything if m["collection"] == "ipl"])


def test_a_search_with_no_match_is_an_empty_list_with_no_cursor(api):
    page = api.get("/v1/matches", params={"q": "zzzzqqq"}).json()
    assert page["data"] == [] and page["next_cursor"] is None


def test_wildcard_characters_in_a_search_mean_themselves(api):
    for text in ("%", "_", "e%a", "a_b", "\\", "\\%", "%%%%"):
        assert every_match(api, q=text) == [], text


def test_pagination_with_a_search_returns_each_match_once_in_order(api, everything):
    expected = ids(plain(everything, "india"))
    limit = max(1, min(500, len(expected) // 3))
    page_ids, cursor, pages = [], None, 0
    while True:
        page = api.get("/v1/matches", params={"q": "india", "limit": limit, **({"cursor": cursor} if cursor else {})}).json()
        assert len(page["data"]) <= limit
        page_ids += ids(page["data"])
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert page_ids == expected and len(page_ids) == len(set(page_ids))
    assert pages >= 3, "a search that fills more than one page"


def test_a_search_combines_with_the_other_filters(api, everything):
    got = ids(every_match(api, q="england", format="TEST", gender="male"))
    assert got == ids([m for m in plain(everything, "england") if m["format"] == "TEST" and m["gender"] == "male"])
    assert ids(every_match(api, q="england", date_from="2019-01-01", date_to="2019-12-31")) == ids(
        [m for m in plain(everything, "england") if "2019-01-01" <= m["start_date"] <= "2019-12-31"])
    assert ids(every_match(api, q="2019", team="England", venue="Headingley")) == ids(
        [m for m in plain(everything, "2019") if "England" in (m["team_1"], m["team_2"]) and m["venue"] == "Headingley"])


def test_the_search_bounds(api):
    assert api.get("/v1/matches", params={"q": ""}).status_code == 422
    assert api.get("/v1/matches", params={"q": "   "}).status_code == 422, "only spaces is no search"
    assert api.get("/v1/matches", params={"q": "x" * (search.MAX_QUERY + 1)}).status_code == 422
    assert api.get("/v1/matches", params={"q": "a" * (search.MAX_KEYWORD + 1)}).status_code == 422, "one word is too long"
    assert api.get("/v1/matches", params={"q": " ".join("w%d" % i for i in range(search.MAX_KEYWORDS + 1))}).status_code == 422, "too many words"
    assert api.get("/v1/matches", params={"q": " ".join("w%d" % i for i in range(search.MAX_KEYWORDS))}).status_code == 200
    assert api.get("/v1/matches", params={"q": "a" * search.MAX_KEYWORD}).status_code == 200
    assert api.get("/v1/competitions", params={"q": ""}).status_code == 422 and api.get("/v1/competitions", params={"q": "a" * 200}).status_code == 422


def test_repeated_keywords_count_once_against_the_limit_on_words():
    assert search.keywords("a A a b") == ["a", "b"]
    assert search.keywords("Ｅngland") == ["England"], "full width letters are normalised"


# The opponent filter --------------------------------------------------------------------------------------------------

def test_opponent_selects_a_pairing_whichever_side_each_team_was_on(api, everything):
    pairing = [m for m in everything if {m["team_1"], m["team_2"]} == {"England", "Australia"}]
    assert pairing and {m["team_1"] for m in pairing} == {"England", "Australia"}, "both orders occur in the data"
    one = every_match(api, team="England", opponent="Australia")
    other = every_match(api, team="Australia", opponent="England")
    assert ids(one) == ids(other) == ids(pairing)


def test_opponent_combines_with_the_other_filters_and_a_search(api, everything):
    tests = [m for m in everything if {m["team_1"], m["team_2"]} == {"England", "Australia"} and m["format"] == "TEST"]
    assert ids(every_match(api, team="England", opponent="Australia", format="TEST")) == ids(tests)
    assert ids(every_match(api, team="England", opponent="Australia", q="headingley 2019")) == ids([m for m in tests if m in plain(tests, "headingley 2019")])
    assert HEADINGLEY in ids(every_match(api, team="Australia", opponent="England", q="headingley 2019"))


def test_opponent_needs_a_team_and_a_team_cannot_play_itself(api):
    response = api.get("/v1/matches", params={"opponent": "Australia"})
    assert response.status_code == 422 and response.json()["parameter"] == "opponent"
    assert every_match(api, team="England", opponent="England") == []
    assert every_match(api, team="England", opponent="Nowhere Rovers") == []
    assert api.get("/v1/matches", params={"team": "England", "opponent": ""}).status_code == 422


def test_a_search_for_a_team_name_and_the_team_filter_give_the_matches_of_that_team(api, everything):
    """q is a piece of a name and team is the whole name, so a full team name through q also finds teams that contain it."""
    exact = ids(every_match(api, team="Australia"))
    by_search = ids(every_match(api, q="Australia"))
    assert set(exact) <= set(by_search)


# Competitions ---------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def competitions(api):
    return api.get("/v1/competitions").json()["data"]


@pytest.mark.parametrize("text", ["ipl", "IPL", "premier", "premier league", "world cup", "tests", "odis", "t20", "women", "zzzzqqq", "bbl big", "%", "_"])
def test_a_competition_search_gives_exactly_what_a_plain_filter_gives(api, competitions, text):
    assert api.get("/v1/competitions", params={"q": text}).json()["data"] == plain(competitions, text, COMPETITION_FIELDS), text


def test_a_competition_search_finds_a_collection_code_and_an_event_name(api, competitions):
    assert [c["collection"] for c in api.get("/v1/competitions", params={"q": "ipl"}).json()["data"]] == ["ipl"], "the code, which is not in the event name Indian Premier League"
    premier = {c["collection"] for c in api.get("/v1/competitions", params={"q": "premier"}).json()["data"]}
    assert premier == {c["collection"] for c in competitions if "premier" in (c["event"] or "").lower() or "premier" in c["collection"]} and "ipl" in premier
    if is_full(api):
        assert {"ipl", "bpl", "cpl"} <= premier
    assert api.get("/v1/competitions", params={"q": "zzzzqqq"}).json()["data"] == []


def test_a_competition_search_combines_with_the_collection_filter_and_changes_nothing_without_q(api, competitions):
    assert api.get("/v1/competitions").json()["data"] == competitions
    both = api.get("/v1/competitions", params={"q": "premier", "collection": "ipl"}).json()["data"]
    assert [c["collection"] for c in both] == ["ipl"]
    assert api.get("/v1/competitions", params={"q": "ipl", "collection": "bbl"}).json()["data"] == []


def test_search_results_carry_the_names_and_ids_for_the_next_request(api):
    match = api.get("/v1/matches", params={"q": "England Australia Headingley 2019"}).json()["data"][0]
    assert match["match_id"] == HEADINGLEY and match["team_1"] and match["venue"]
    assert api.get(f"/v1/matches/{match['match_id']}/scorecard").status_code == 200
    ids_by_name = {p["name"]: p["player_id"] for p in api.get("/v1/players", params={"q": "Stokes"}).json()["data"]}
    assert "BA Stokes" in ids_by_name, "players are found by name and then used by id"


def test_searching_does_not_change_the_other_lookups(api):
    assert api.get("/v1/teams", params={"q": "austral"}).status_code == 200
    assert api.get("/v1/venues", params={"q": "headingley"}).json()["data"][0]["venue"].lower().startswith("headingley")
    assert "BA Stokes" in [p["name"] for p in api.get("/v1/players", params={"q": "stokes"}).json()["data"]]
    assert len(api.get("/v1/matches", params={"limit": 3}).json()["data"]) == 3
