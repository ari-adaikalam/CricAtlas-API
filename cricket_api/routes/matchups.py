"""One batter against one bowler. The definitions are in metrics.matchup."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from cricket_api import metrics
from cricket_api.http import respond, strict
from cricket_api.models import MatchupResponse
from cricket_api.openapi import errors
from cricket_api.routes.lookups import Collection
from cricket_api.routes.players import PlayerId, player_name

router = APIRouter(prefix="/v1")
ALLOWED = {"batter", "bowler", "collection", "format", "gender", "season", "date_from", "date_to"}


@router.get("/matchups", summary="One batter against one bowler, with filters", response_model=MatchupResponse, responses=errors(404))
def matchups(request: Request, batter: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{1,16}$")], bowler: Annotated[str, Query(pattern=r"^[A-Za-z0-9_-]{1,16}$")],
             collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None, gender: Literal["male", "female"] | None = None,
             season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, date_from: date | None = None, date_to: date | None = None):
    strict(request, ALLOWED)
    batter_name, bowler_name = player_name(request, batter), player_name(request, bowler)
    matches_sql, params = metrics.selected_matches(collection, format, gender, season, date_from, date_to)
    figures = metrics.matchup(request.app.state.db, matches_sql, params, batter, bowler)
    return respond(request, MatchupResponse, {"data": {"batter": {"player_id": batter, "name": batter_name}, "bowler": {"player_id": bowler, "name": bowler_name}, **figures}})
