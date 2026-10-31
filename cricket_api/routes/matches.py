"""Matches: a filtered list, one match with its innings, and the balls of an innings."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from cricket_api import contract, cursors, names, scorecard, search
from cricket_api.errors import ApiError
from cricket_api.http import make_cursor, read_cursor, respond, strict
from cricket_api.models import BallsResponse, MatchesResponse, MatchResponse, ScorecardResponse
from cricket_api.openapi import errors
from cricket_api.routes.lookups import Collection
from cricket_api.scorecard import innings_totals

router = APIRouter(prefix="/v1")

MatchId = Annotated[int, Path(ge=1, le=2_147_483_647)]
InningsNumber = Annotated[int, Path(ge=1, le=8)]
BALL_FIELDS = [names.public(c) for c in contract.BALLS]
BALL_DEFAULT = ["innings", "delivery_number", "over", "ball_in_over", "scorecard_ball", "is_legal_delivery", "batting_team", "bowling_team", "striker", "striker_id",
                "non_striker", "bowler", "bowler_id", "runs_off_bat", "extras", "wides", "noballs", "byes", "legbyes", "penalty", "non_boundary", "wicket",
                "wicket_type", "player_dismissed", "fielder_1"]


MATCH_SEARCH_FIELDS = ("team_1", "team_2", "event", "collection", "venue", "city", "season")


@router.get("/matches", summary="Matches, newest first, filtered, searchable and paged", response_model=MatchesResponse, responses=errors())
def list_matches(request: Request, collection: Collection | None = None, event: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
                 season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                 gender: Literal["male", "female"] | None = None, team: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
                 opponent: Annotated[str | None, Query(min_length=1, max_length=120, description="With team: only matches between the two, whichever side each was on")] = None,
                 venue: Annotated[str | None, Query(min_length=1, max_length=160)] = None, date_from: date | None = None, date_to: date | None = None,
                 q: Annotated[str | None, Query(min_length=1, max_length=search.MAX_QUERY,
                                                description="Keywords, each of which must be part of a team, event, collection code, venue, city or season")] = None,
                 limit: Annotated[int, Query(ge=1, le=500)] = 50, cursor: Annotated[str | None, Query(max_length=400)] = None):
    strict(request, {"collection", "event", "season", "format", "gender", "team", "opponent", "venue", "date_from", "date_to", "q", "limit", "cursor"})
    conditions, params = [], []
    for column, value in (("collection", collection), ("event", event), ("season", season), ("format", format), ("gender", gender), ("venue", venue)):
        if value is not None:
            conditions.append(f"{column} = ?")
            params.append(value)
    if opponent is not None and team is None:
        raise ApiError(422, "opponent needs team", parameter="opponent")
    if team is not None and opponent is not None:
        conditions.append("((team_1 = ? AND team_2 = ?) OR (team_1 = ? AND team_2 = ?))")
        params += [team, opponent, opponent, team]
    elif team is not None:
        conditions.append("(team_1 = ? OR team_2 = ?)")
        params += [team, team]
    if q is not None:
        found, found_params = search.clause(q, MATCH_SEARCH_FIELDS)
        conditions.append(found)
        params += found_params
    if date_from is not None:
        conditions.append("start_date >= ?")
        params.append(date_from)
    if date_to is not None:
        conditions.append("start_date <= ?")
        params.append(date_to)
    if cursor:
        day, match_id = read_cursor(request, "matches", cursor, cursors.DATE_AND_MATCH)
        conditions.append("(start_date, match_id) < (?, ?)")
        params += [day, match_id]
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = request.app.state.db.query(f"SELECT * FROM matches {where} ORDER BY start_date DESC, match_id DESC LIMIT ?", params + [limit + 1])
    more = rows[limit:]
    rows = rows[:limit]
    next_cursor = make_cursor(request, "matches", [rows[-1]["start_date"].isoformat(), rows[-1]["match_id"]]) if more else None
    return respond(request, MatchesResponse, {"data": rows, "next_cursor": next_cursor})


def fetch_match(request: Request, match_id: int) -> dict:
    rows = request.app.state.db.query("SELECT * FROM matches WHERE match_id = ?", [match_id])
    if not rows:
        raise ApiError(404, "Match not found")
    return rows[0]


@router.get("/matches/{match_id}", summary="One match: its details, innings totals, squads and officials", response_model=MatchResponse, responses=errors(404))
def get_match(request: Request, match_id: MatchId):
    strict(request, set())
    db = request.app.state.db
    match = fetch_match(request, match_id)
    innings = innings_totals(db, match_id, match["balls_per_over"] or 6)
    people = db.query("SELECT role, person_number, team, person, person_id FROM match_people WHERE match_id = ? ORDER BY role, person_number", [match_id])
    squads: dict[str, list] = {}
    for p in people:
        if p["role"] == "player":
            squads.setdefault(p["team"], []).append({"player_id": p["person_id"], "name": p["person"]})
    officials = [{"role": p["role"], "name": p["person"], "person_id": p["person_id"]} for p in people if p["role"] != "player"]
    return respond(request, MatchResponse, {"data": {**match, "innings": innings, "squads": squads, "officials": officials}})


@router.get("/matches/{match_id}/scorecard", summary="The scorecard: batting, bowling, extras and fall of wickets for every innings", response_model=ScorecardResponse,
            responses=errors(404))
def get_scorecard(request: Request, match_id: MatchId):
    strict(request, set())
    fetch_match(request, match_id)
    return respond(request, ScorecardResponse, {"data": scorecard.stored(request.app.state.db, match_id)})


@router.get("/matches/{match_id}/innings/{innings}/balls", summary="The balls of an innings, in order, paged", response_model=BallsResponse, responses=errors(404))
def innings_balls(request: Request, match_id: MatchId, innings: InningsNumber, fields: Annotated[str | None, Query(max_length=600)] = None,
                  limit: Annotated[int, Query(ge=1, le=1000)] = 100, cursor: Annotated[str | None, Query(max_length=400)] = None):
    strict(request, {"fields", "limit", "cursor"})
    chosen = BALL_DEFAULT if not fields else [f.strip() for f in fields.split(",") if f.strip()]
    unknown = [f for f in chosen if f not in BALL_FIELDS]
    if unknown:
        raise ApiError(400, f"Unknown field: {unknown[0][:40]}", parameter="fields")
    if "delivery_number" not in chosen:
        chosen = ["delivery_number", *chosen]
    columns = ", ".join('"' + names.column(f) + '"' for f in dict.fromkeys(chosen))
    conditions, params = ["match_id = ?", "innings = ?"], [match_id, innings]
    if cursor:
        conditions.append("delivery_number > ?")
        params.append(read_cursor(request, f"balls/{match_id}/{innings}", cursor, cursors.DELIVERY_NUMBER)[0])
    db = request.app.state.db
    rows = db.query(f"SELECT {columns} FROM balls WHERE {' AND '.join(conditions)} ORDER BY delivery_number LIMIT ?", params + [limit + 1])
    if not rows and not cursor:
        fetch_match(request, match_id)
        raise ApiError(404, "Innings not found")
    more = rows[limit:]
    rows = [names.rename(r) for r in rows[:limit]]
    next_cursor = make_cursor(request, f"balls/{match_id}/{innings}", [rows[-1]["delivery_number"]]) if more else None
    return respond(request, BallsResponse, {"data": rows, "next_cursor": next_cursor}, row_order=list(rows[0]) if rows else None)
