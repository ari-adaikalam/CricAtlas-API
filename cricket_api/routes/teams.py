"""Team records and head-to-head records. The counting rules are in records.py."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from cricket_api import cursors, records
from cricket_api.errors import ApiError
from cricket_api.http import make_cursor, read_cursor, respond, strict
from cricket_api.models import (HeadToHeadBy, HeadToHeadResponse, HeadToHeadSplitsResponse, RecordBy, TeamRecordResponse, TeamSplitsResponse)
from cricket_api.openapi import errors
from cricket_api.routes.lookups import Collection

router = APIRouter(prefix="/v1")

Team = Annotated[str, Query(min_length=1, max_length=120, description="A team name as /v1/teams lists it")]
Gender = Annotated[Literal["male", "female"], Query(description="The gender of the cricket. A team is its name and this, because the same name is used in both")]
FILTERS = {"gender", "collection", "format", "season", "date_from", "date_to"}
Limit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=400)]


def check_teams(request: Request, team: str, opponent: str | None, gender: str) -> None:
    """The teams are checked against their name and gender before any statistical filter. The same team on both sides is a 422, and an unknown team is a 404."""
    if opponent is not None and team == opponent:
        raise ApiError(422, "A team cannot be its own opponent", parameter="opponent")
    db = request.app.state.db
    records.require_team(db, team, gender, "team")
    if opponent is not None:
        records.require_team(db, opponent, gender, "opponent")


def order(row: dict) -> tuple:
    """Most matches first, then the key from A to Z, and a group with no key last among those with the same number of matches."""
    return (-row["matches"], row["key"] is None, row["key"] or "")


def page(request: Request, rows: list[dict], dimension: str, route: str, limit: int, cursor: str | None):
    for row in rows:
        row["label"] = records.split_label(dimension, row["key"])
    rows.sort(key=order)
    if cursor:
        matches, key = read_cursor(request, route, cursor, cursors.COUNT_AND_KEY)
        after = (-matches, key is None, key or "")
        rows = [r for r in rows if order(r) > after]
    more = rows[limit:]
    rows = rows[:limit]
    return rows, (make_cursor(request, route, [rows[-1]["matches"], rows[-1]["key"]]) if more else None)


@router.get("/teams/record", summary="A team's results: wins, losses, draws, ties and no results", response_model=TeamRecordResponse, responses=errors(404))
def team_record(request: Request, team: Team, gender: Gender, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, date_from: date | None = None, date_to: date | None = None):
    strict(request, {"team"} | FILTERS)
    check_teams(request, team, None, gender)
    sql, params = records.selected(gender, team, None, collection, format, season, date_from, date_to)
    return respond(request, TeamRecordResponse, {"data": {"team": team, "gender": gender, **records.overall(request.app.state.db, sql, params, team)}})


@router.get("/teams/record/splits", summary="A team's results by opponent, venue, competition, format or season", response_model=TeamSplitsResponse, responses=errors(404))
def team_record_splits(request: Request, team: Team, gender: Gender, by: RecordBy, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                       season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, date_from: date | None = None, date_to: date | None = None,
                       limit: Limit = 100, cursor: Cursor = None):
    strict(request, {"team", "by", "limit", "cursor"} | FILTERS)
    check_teams(request, team, None, gender)
    db = request.app.state.db
    sql, params = records.selected(gender, team, None, collection, format, season, date_from, date_to)
    whole = records.overall(db, sql, params, team)
    rows, next_cursor = page(request, records.groups(db, sql, params, team, by), by, f"teams/record/{gender}/{team}/{by}", limit, cursor)
    return respond(request, TeamSplitsResponse, {"data": {"team": team, "gender": gender, "by": by, "coverage": whole["coverage"], "groups": rows}, "next_cursor": next_cursor})


@router.get("/head-to-head", summary="One team's record against another, whichever side each was on", response_model=HeadToHeadResponse, responses=errors(404))
def head_to_head(request: Request, team: Team, opponent: Team, gender: Gender, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                 season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, date_from: date | None = None, date_to: date | None = None):
    strict(request, {"team", "opponent"} | FILTERS)
    check_teams(request, team, opponent, gender)
    sql, params = records.selected(gender, team, opponent, collection, format, season, date_from, date_to)
    return respond(request, HeadToHeadResponse, {"data": {"team": team, "opponent": opponent, "gender": gender, **records.overall(request.app.state.db, sql, params, team)}})


@router.get("/head-to-head/splits", summary="A head-to-head record by venue, competition, format or season", response_model=HeadToHeadSplitsResponse, responses=errors(404))
def head_to_head_splits(request: Request, team: Team, opponent: Team, gender: Gender, by: HeadToHeadBy, collection: Collection | None = None,
                        format: Literal["TEST", "ODI", "T20"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
                        date_from: date | None = None, date_to: date | None = None, limit: Limit = 100, cursor: Cursor = None):
    strict(request, {"team", "opponent", "by", "limit", "cursor"} | FILTERS)
    check_teams(request, team, opponent, gender)
    db = request.app.state.db
    sql, params = records.selected(gender, team, opponent, collection, format, season, date_from, date_to)
    whole = records.overall(db, sql, params, team)
    rows, next_cursor = page(request, records.groups(db, sql, params, team, by), by, f"head-to-head/{gender}/{team}/{opponent}/{by}", limit, cursor)
    return respond(request, HeadToHeadSplitsResponse, {"data": {"team": team, "opponent": opponent, "gender": gender, "by": by, "coverage": whole["coverage"], "groups": rows},
                                                       "next_cursor": next_cursor})
