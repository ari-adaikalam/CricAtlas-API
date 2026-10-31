"""A player's career batting, bowling and fielding, and the same by season, over the matches the filters select.

The totals and rates come from metrics.py, which is where the rules are written. In short:

  A Super Over is not part of a career: its runs, balls, wickets and innings are left out, as official records do.
  Innings batted: the player faced a ball, stood at the non-striker's end for one, or was dismissed.
  Not out: an innings in which the player was not dismissed. Retired hurt and retired not out count as not out. Retired out is a dismissal.
  Average: runs per dismissal. Strike rate: runs per 100 balls faced.
  Bowling economy: runs per six balls, in every format. Strike rate: balls per wicket. Best figures: most wickets in an innings, then fewest runs.
  Maidens follow maidens.py: confirmed complete overs bowled whole by the player with no run charged, by the umpires' count, so a five-ball Hundred set counts.
  A known player is any id the data names as a player (see player_universe.py), so a squad member who never batted, bowled or fielded gets 200 with zero
  totals and null rates. Only an id nobody has heard of is a 404.
  Fielding counts distinct wickets per player, Super Overs left out: catches by a fielder, caught and bowled (the catcher is the bowler, and these wickets carry
  no fielder row), stumpings, and run out involvements (every named fielder gets one, which is not a direct hit). A substitute fielder's wickets are counted
  apart as substitute involvements and are in none of the other counts.
"""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Request

from cricket_api.errors import ApiError
from cricket_api.http import make_cursor, read_cursor, respond, strict
from cricket_api import cursors, history, metrics
from cricket_api.models import (BattingResponse, BattingSeasonsResponse, BattingSplitsResponse, BowlingResponse, BowlingSeasonsResponse, BowlingSplitsResponse,
                                FieldingResponse, FieldingSeasonsResponse, FieldingSplitsResponse, PlayerMatchesResponse, PlayerResponse, SplitBy)
from cricket_api.openapi import errors
from cricket_api.routes.lookups import Collection

router = APIRouter(prefix="/v1")

PlayerId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,16}$")]
FILTERS = {"collection", "format", "gender", "season", "date_from", "date_to"}
# a season route groups by season, so it takes every filter except the season
SEASON_FILTERS = FILTERS - {"season"}
MAX_SEASONS = 100


selected_matches = metrics.selected_matches


def player_name(request: Request, player_id: str) -> str | None:
    rows = request.app.state.db.query("SELECT name FROM players WHERE player_id = ?", [player_id])
    if not rows:
        raise ApiError(404, "Player not found")
    return rows[0]["name"]


@router.get("/players/{player_id}/batting", summary="A player's career batting, with filters", response_model=BattingResponse, responses=errors(404))
def batting(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
            gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
            date_from: date | None = None, date_to: date | None = None):
    strict(request, FILTERS)
    name = player_name(request, player_id)
    matches_sql, params = selected_matches(collection, format, gender, season, date_from, date_to)
    row = metrics.batting(request.app.state.db, matches_sql, params, player_id)[0]
    return respond(request, BattingResponse, {"data": {"player_id": player_id, "name": name, **row}})


@router.get("/players/{player_id}/bowling", summary="A player's career bowling, with filters", response_model=BowlingResponse, responses=errors(404))
def bowling(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
            gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
            date_from: date | None = None, date_to: date | None = None):
    strict(request, FILTERS)
    name = player_name(request, player_id)
    matches_sql, params = selected_matches(collection, format, gender, season, date_from, date_to)
    row = metrics.bowling(request.app.state.db, matches_sql, params, player_id)[0]
    return respond(request, BowlingResponse, {"data": {"player_id": player_id, "name": name, **row}})


@router.get("/players/{player_id}", summary="A player: name, other spellings, teams and ids in other systems", response_model=PlayerResponse, responses=errors(404))
def profile(request: Request, player_id: PlayerId):
    strict(request, set())
    name = player_name(request, player_id)
    q = request.app.state.db.query
    aliases = [r["name"] for r in q("SELECT name FROM player_names WHERE player_id = ? AND name <> ? ORDER BY uses DESC, name", [player_id, name or ""])]
    teams = q("SELECT p.team, count(DISTINCT p.match_id) AS matches, min(m.start_date) AS first_match, max(m.start_date) AS last_match FROM match_people p "
              "JOIN matches m ON m.match_id = p.match_id WHERE p.person_id = ? AND p.role = 'player' GROUP BY p.team ORDER BY matches DESC, p.team", [player_id])
    external = q("SELECT source, external_id FROM player_external_ids WHERE player_id = ? ORDER BY source, external_id", [player_id])
    return respond(request, PlayerResponse, {"data": {"player_id": player_id, "name": name, "aliases": aliases, "teams": teams, "external_ids": external}})


@router.get("/players/{player_id}/fielding", summary="A player's career fielding, with filters", response_model=FieldingResponse, responses=errors(404))
def fielding(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
             gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
             date_from: date | None = None, date_to: date | None = None):
    strict(request, FILTERS)
    name = player_name(request, player_id)
    matches_sql, params = selected_matches(collection, format, gender, season, date_from, date_to)
    row = metrics.fielding(request.app.state.db, matches_sql, params, player_id)[0]
    return respond(request, FieldingResponse, {"data": {"player_id": player_id, "name": name, **row}})


def by_season(request: Request, response_model, kind: str, player_id: str, collection, format, gender, date_from, date_to):
    strict(request, SEASON_FILTERS)
    name = player_name(request, player_id)
    matches_sql, params = selected_matches(collection, format, gender, None, date_from, date_to)
    rows = getattr(metrics, kind)(request.app.state.db, matches_sql, params, player_id, by="season")[:MAX_SEASONS]
    return respond(request, response_model, {"data": {"player_id": player_id, "name": name, "seasons": rows}})


@router.get("/players/{player_id}/batting/seasons", summary="A player's batting season by season, oldest first", response_model=BattingSeasonsResponse, responses=errors(404))
def batting_seasons(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                    gender: Literal["male", "female"] | None = None, date_from: date | None = None, date_to: date | None = None):
    return by_season(request, BattingSeasonsResponse, "batting", player_id, collection, format, gender, date_from, date_to)


@router.get("/players/{player_id}/bowling/seasons", summary="A player's bowling season by season, oldest first", response_model=BowlingSeasonsResponse, responses=errors(404))
def bowling_seasons(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                    gender: Literal["male", "female"] | None = None, date_from: date | None = None, date_to: date | None = None):
    return by_season(request, BowlingSeasonsResponse, "bowling", player_id, collection, format, gender, date_from, date_to)


@router.get("/players/{player_id}/fielding/seasons", summary="A player's fielding season by season, oldest first", response_model=FieldingSeasonsResponse, responses=errors(404))
def fielding_seasons(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                     gender: Literal["male", "female"] | None = None, date_from: date | None = None, date_to: date | None = None):
    return by_season(request, FieldingSeasonsResponse, "fielding", player_id, collection, format, gender, date_from, date_to)


@router.get("/players/{player_id}/matches", summary="The matches a player is connected to, newest first, with what the records say about their part", response_model=PlayerMatchesResponse,
            responses=errors(404))
def player_history(request: Request, player_id: PlayerId, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                   gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
                   date_from: date | None = None, date_to: date | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50,
                   cursor: Annotated[str | None, Query(max_length=400)] = None):
    strict(request, FILTERS | {"limit", "cursor"})
    player_name(request, player_id)
    matches_sql, params = selected_matches(collection, format, gender, season, date_from, date_to)
    after = None
    if cursor:
        day, match_id = read_cursor(request, f"players/{player_id}/matches", cursor, cursors.DATE_AND_MATCH)
        after = (day, match_id)
    rows = history.player_matches(request.app.state.db, matches_sql, params, player_id, after, limit + 1)
    more = rows[limit:]
    rows = rows[:limit]
    next_cursor = make_cursor(request, f"players/{player_id}/matches", [rows[-1]["start_date"].isoformat(), rows[-1]["match_id"]]) if more else None
    return respond(request, PlayerMatchesResponse, {"data": rows, "next_cursor": next_cursor})


def split_route(request: Request, response_model, kind: str, player_id: str, by: str, filters: tuple, limit: int, cursor: str | None):
    strict(request, FILTERS | {"by", "limit", "cursor"})
    name = player_name(request, player_id)
    matches_sql, params = selected_matches(*filters)
    rows = metrics.splits(request.app.state.db, kind, matches_sql, params, player_id, by)
    route = f"players/{player_id}/{kind}/splits/{by}"
    if cursor:
        matches, key = read_cursor(request, route, cursor, cursors.COUNT_AND_KEY)
        after = (-matches, key is None, key or "")
        rows = [r for r in rows if metrics.split_order(r) > after]
    more = rows[limit:]
    rows = rows[:limit]
    next_cursor = make_cursor(request, route, [rows[-1]["matches"], rows[-1]["key"]]) if more else None
    return respond(request, response_model, {"data": {"player_id": player_id, "name": name, "by": by, "groups": rows}, "next_cursor": next_cursor})


@router.get("/players/{player_id}/batting/splits", summary="A player's batting by opponent, venue, competition or format", response_model=BattingSplitsResponse, responses=errors(404))
def batting_splits(request: Request, player_id: PlayerId, by: SplitBy, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                   gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
                   date_from: date | None = None, date_to: date | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 100, cursor: Annotated[str | None, Query(max_length=400)] = None):
    return split_route(request, BattingSplitsResponse, "batting", player_id, by, (collection, format, gender, season, date_from, date_to), limit, cursor)


@router.get("/players/{player_id}/bowling/splits", summary="A player's bowling by opponent, venue, competition or format", response_model=BowlingSplitsResponse, responses=errors(404))
def bowling_splits(request: Request, player_id: PlayerId, by: SplitBy, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                   gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
                   date_from: date | None = None, date_to: date | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 100, cursor: Annotated[str | None, Query(max_length=400)] = None):
    return split_route(request, BowlingSplitsResponse, "bowling", player_id, by, (collection, format, gender, season, date_from, date_to), limit, cursor)


@router.get("/players/{player_id}/fielding/splits", summary="A player's fielding by opponent, venue, competition or format", response_model=FieldingSplitsResponse, responses=errors(404))
def fielding_splits(request: Request, player_id: PlayerId, by: SplitBy, collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None,
                    gender: Literal["male", "female"] | None = None, season: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
                    date_from: date | None = None, date_to: date | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 100, cursor: Annotated[str | None, Query(max_length=400)] = None):
    return split_route(request, FieldingSplitsResponse, "fielding", player_id, by, (collection, format, gender, season, date_from, date_to), limit, cursor)
