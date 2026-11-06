"""Lists to look things up in: competitions, teams, venues and players."""
import unicodedata
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from cricket_api.errors import ApiError
from cricket_api import search
from cricket_api.http import escape_like, respond, strict
from cricket_api.models import CompetitionsResponse, PlayersResponse, TeamsResponse, VenuesResponse
from cricket_api.openapi import errors

router = APIRouter(prefix="/v1")

Collection = Literal["tests", "odis", "t20s", "ipl", "wpl", "bbl", "wbb", "cpl", "psl", "sat", "ilt", "hnd", "mlc", "lpl", "bpl"]


def search_text(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text).strip()
    if not cleaned:
        raise ApiError(422, "The search text is empty", parameter="q")
    return escape_like(cleaned)


@router.get("/competitions", summary="Competitions with their match counts and dates, searchable by name", response_model=CompetitionsResponse, responses=errors())
def competitions(request: Request, collection: Collection | None = None,
                 q: Annotated[str | None, Query(min_length=1, max_length=search.MAX_QUERY, description="Keywords, all of which must be part of the event name or the collection code")] = None):
    strict(request, {"collection", "q"})
    conditions, params = [], []
    if collection:
        conditions.append("collection = ?")
        params.append(collection)
    if q is not None:
        found, found_params = search.clause(q, ("event", "collection"))
        conditions.append(found)
        params += found_params
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = request.app.state.db.query(
        f"SELECT collection, event, type, format, gender, count(*) AS matches, min(start_date) AS first_match, max(start_date) AS last_match "
        f"FROM matches {where} GROUP BY collection, event, type, format, gender ORDER BY collection, event NULLS LAST, gender", params)
    return respond(request, CompetitionsResponse, {"data": rows})


def named_list(request: Request, model, column: str, q: str | None, collection: str | None, limit: int, gender: str | None = None):
    """Teams or venues with their match counts. A team is its name and the gender of the cricket, so the same name in men's and women's cricket is two entries."""
    team = column == "team"
    union = "SELECT team_1 AS name, gender, collection FROM matches UNION ALL SELECT team_2, gender, collection FROM matches" if team else \
            "SELECT venue AS name, gender, collection FROM matches"
    conditions, params = ["name IS NOT NULL"], []
    if collection:
        conditions.append("collection = ?")
        params.append(collection)
    if gender:
        conditions.append("gender = ?")
        params.append(gender)
    if q:
        conditions.append("name ILIKE ? ESCAPE '\\'")
        params.append(f"%{search_text(q)}%")
    columns, order = ("name, gender", "matches DESC, name, gender") if team else ("name", "matches DESC, name")
    rows = request.app.state.db.query(f"WITH t AS ({union}) SELECT {columns}, count(*) AS matches FROM t WHERE {' AND '.join(conditions)} GROUP BY {columns} ORDER BY {order} LIMIT ?", params + [limit])
    if team:
        return respond(request, model, {"data": [{"team": r["name"], "gender": r["gender"], "matches": r["matches"]} for r in rows]})
    return respond(request, model, {"data": [{column: r["name"], "matches": r["matches"]} for r in rows]})


@router.get("/teams", summary="Teams with their match counts, one entry for each team and gender", response_model=TeamsResponse, responses=errors())
def teams(request: Request, q: Annotated[str | None, Query(min_length=1, max_length=60)] = None, collection: Collection | None = None,
          gender: Literal["male", "female"] | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    strict(request, {"q", "collection", "gender", "limit"})
    return named_list(request, TeamsResponse, "team", q, collection, limit, gender)


@router.get("/venues", summary="Venues with their match counts", response_model=VenuesResponse, responses=errors())
def venues(request: Request, q: Annotated[str | None, Query(min_length=1, max_length=60)] = None, collection: Collection | None = None,
           limit: Annotated[int, Query(ge=1, le=500)] = 100):
    strict(request, {"q", "collection", "limit"})
    return named_list(request, VenuesResponse, "venue", q, collection, limit)


@router.get("/players", summary="Search players by any spelling of their name", response_model=PlayersResponse, responses=errors())
def players(request: Request, q: Annotated[str, Query(min_length=2, max_length=60)], limit: Annotated[int, Query(ge=1, le=100)] = 25):
    strict(request, {"q", "limit"})
    rows = request.app.state.db.query(
        "SELECT p.player_id, p.name FROM players p WHERE p.player_id IN (SELECT player_id FROM player_names WHERE name ILIKE ? ESCAPE '\\') ORDER BY p.name, p.player_id LIMIT ?",
        [f"%{search_text(q)}%", limit])
    return respond(request, PlayersResponse, {"data": rows})
