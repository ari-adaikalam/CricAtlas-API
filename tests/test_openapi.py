"""The OpenAPI document is the contract, so it has to be complete and it has to be true."""
import pytest
from pydantic import ValidationError

from cricket_api import contract, models, names
from cricket_api.errors import PROBLEM


@pytest.fixture
def schema(client):
    return client.get("/openapi.json").json()


def operations(schema):
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            yield path, method, operation


def test_only_get_routes_are_documented(schema):
    assert {m for _, m, _ in operations(schema)} == {"get"}


def test_every_200_response_has_a_concrete_json_schema(schema):
    for path, _, operation in operations(schema):
        content = operation["responses"]["200"]["content"]["application/json"]
        ref = content["schema"].get("$ref")
        assert ref, f"{path} has no named response schema"
        assert ref.rsplit("/", 1)[1] in schema["components"]["schemas"]


def test_every_error_is_problem_json_with_the_problem_model(schema):
    for path, _, operation in operations(schema):
        errors = {code: r for code, r in operation["responses"].items() if code[0] in "45"}
        assert {"400", "429", "500"} <= set(errors) or path == "/health", path
        for code, response in errors.items():
            assert list(response["content"]) == [PROBLEM], (path, code)
            assert response["content"][PROBLEM]["schema"]["$ref"].endswith("/Problem"), (path, code)


def test_the_default_validation_error_shape_is_gone(schema):
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "HTTPValidationError" not in str(schema["paths"])


def test_the_document_carries_the_licence_and_attribution(schema):
    assert schema["info"]["license"]["url"] == "https://opendatacommons.org/licenses/by/1-0/"
    assert schema["info"]["contact"] == {"name": "Ari Adaikalam", "url": "https://ariadaikalam.com/", "email": "ariadaikalam1234@gmail.com"}      # the document's URL type adds the closing slash
    assert "Cricsheet" in schema["info"]["description"]


def test_every_route_is_listed(schema):
    assert set(schema["paths"]) == {
        "/health", "/status", "/freshness", "/v1/meta", "/v1/competitions", "/v1/teams", "/v1/venues", "/v1/players", "/v1/matches", "/v1/matches/{match_id}",
        "/v1/matches/{match_id}/scorecard", "/v1/matches/{match_id}/innings/{innings}/balls", "/v1/players/{player_id}", "/v1/players/{player_id}/fielding", "/v1/leaderboards/batting", "/v1/leaderboards/bowling", "/v1/matchups", "/v1/teams/record", "/v1/teams/record/splits", "/v1/head-to-head", "/v1/head-to-head/splits", "/v1/players/{player_id}/matches", "/v1/players/{player_id}/batting/splits", "/v1/players/{player_id}/bowling/splits", "/v1/players/{player_id}/fielding/splits", "/v1/players/{player_id}/batting", "/v1/players/{player_id}/bowling", "/v1/players/{player_id}/batting/seasons",
        "/v1/players/{player_id}/bowling/seasons", "/v1/players/{player_id}/fielding/seasons",
        "/v1/matches/{match_id}/powerplays", "/v1/matches/{match_id}/reviews", "/v1/matches/{match_id}/replacements", "/v1/matches/{match_id}/wickets", "/v1/matches/{match_id}/notes"}


def test_the_match_and_ball_models_list_exactly_the_contract_columns():
    assert list(models.Match.model_fields) == [names.public(c) for c in contract.MATCHES]
    assert list(models.Ball.model_fields) == [names.public(c) for c in contract.BALLS]


def test_no_banned_name_is_in_the_schema(schema):
    text = str(schema["components"]).lower()
    for word in contract.BANNED_WORDS:
        assert word not in text, word


def test_a_model_rejects_a_field_it_does_not_list():
    with pytest.raises(ValidationError):
        models.Source(name="a", url="b", licence="c", licence_url="d", extra="x")
    with pytest.raises(ValidationError):
        models.Ball(striker="A", wagon_x=3)


def test_a_model_rejects_a_wrong_type():
    with pytest.raises(ValidationError):
        models.InningsTotal(runs="12", wickets=1, overs="1.0", legal_balls=6, run_rate=12.0)
    with pytest.raises(ValidationError):
        models.Competition(collection="x", event=None, type=None, format=None, gender=None, matches=1.5, first_match=None, last_match=None)


@pytest.mark.parametrize("path,status", [
    ("/v1/matches/999", 404), ("/v1/matches/1/innings/5/balls", 404), ("/v1/matches?bogus=1", 400), ("/v1/matches?limit=0", 422), ("/v1/nothing", 404),
    ("/v1/matches/1/innings/1/balls?fields=wagon_x", 400), ("/v1/players?q=a", 422), ("/v1/matches?date_from=notadate", 422)])
def test_real_error_bodies_validate_as_problem_details(client, path, status):
    response = client.get(path)
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM)
    models.Problem.model_validate(response.json())


def test_a_405_and_a_429_are_problem_details_too(make_client):
    assert models.Problem.model_validate(make_client().post("/v1/matches").json()).status == 405
    tight = make_client()
    tight.app.state.limiter = type(tight.app.state.limiter)(per_minute=60, burst=1)
    tight.get("/v1/meta")
    limited = tight.get("/v1/meta")
    assert limited.status_code == 429
    models.Problem.model_validate(limited.json())


def test_health_and_status_follow_their_models(client):
    models.Health.model_validate(client.get("/health").json())
    models.Status.model_validate(client.get("/status").json())



def test_no_route_is_registered_twice(client):
    seen = [(r.path, m) for r in client.app.routes if hasattr(r, "methods") for m in r.methods if m != "HEAD"]
    assert len(seen) == len(set(seen)), [s for s in seen if seen.count(s) > 1]


def test_the_search_parameters_are_documented_with_their_bounds(schema):
    def parameters(path):
        return {p["name"]: p for p in schema["paths"][path]["get"]["parameters"]}
    matches, competitions = parameters("/v1/matches"), parameters("/v1/competitions")
    assert matches["q"]["schema"]["anyOf"][0]["maxLength"] == 120 and "Keywords" in matches["q"]["description"]
    assert "opponent" in matches and "whichever side" in matches["opponent"]["description"]
    assert competitions["q"]["schema"]["anyOf"][0]["maxLength"] == 120
    assert "q" in parameters("/v1/players") and "q" in parameters("/v1/teams") and "q" in parameters("/v1/venues"), "the older searches are still there"
