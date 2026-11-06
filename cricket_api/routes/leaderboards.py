"""Leaderboards for batting and bowling. The rules, the allow-lists and the order are in leaderboards.py."""
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request

from cricket_api import leaderboards, metrics
from cricket_api.http import respond, strict
from cricket_api.models import BattingLeaderboardResponse, BowlingLeaderboardResponse
from cricket_api.openapi import errors
from cricket_api.routes.lookups import Collection

router = APIRouter(prefix="/v1")

FILTERS = {"metric", "limit", "min_balls", "collection", "format", "gender", "season", "date_from", "date_to"}
Threshold = Annotated[int | None, Query(ge=1, le=leaderboards.MAX_THRESHOLD)]
Limit = Annotated[int, Query(ge=1, le=leaderboards.MAX_LIMIT)]
BattingMetric = Literal["runs", "average", "strike_rate", "hundreds", "fifties", "fours", "sixes", "highest_score"]
BowlingMetric = Literal["wickets", "maidens", "five_wicket_hauls", "average", "economy", "strike_rate", "best_figures"]
assert set(BattingMetric.__args__) == set(leaderboards.BATTING) and set(BowlingMetric.__args__) == set(leaderboards.BOWLING)


def leaders(request: Request, kind: str, metric: str, limit: int, min_balls, min_other, filters: tuple):
    matches_sql, params = metrics.selected_matches(*filters)
    default_balls, default_other = leaderboards.defaults(kind, metric)
    balls = min_balls if min_balls is not None else default_balls
    other = min_other if min_other is not None else default_other
    rows = leaderboards.rank(request.app.state.db, kind, matches_sql, params, metric, limit, balls, other)
    stats = metrics.batting_stats if kind == "batting" else metrics.bowling_stats
    chosen = (leaderboards.BATTING if kind == "batting" else leaderboards.BOWLING)[metric]
    return balls, other, [{"rank": r["rank"], "player_id": r["pid"], "name": r["name"], "value": r["shown"], "totals": stats(r)} for r in rows], chosen.order.lower()


@router.get("/leaderboards/batting", summary="Batting leaders for a metric, with eligibility and filters", response_model=BattingLeaderboardResponse, responses=errors())
def batting_leaders(request: Request, metric: BattingMetric, limit: Limit = 25, min_balls: Threshold = None, min_dismissals: Threshold = None,
                    collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None, gender: Literal["male", "female"] | None = None,
                    season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, date_from: date | None = None, date_to: date | None = None):
    strict(request, FILTERS | {"min_dismissals"})
    balls, other, rows, order = leaders(request, "batting", metric, limit, min_balls, min_dismissals, (collection, format, gender, season, date_from, date_to))
    return respond(request, BattingLeaderboardResponse, {"data": {"metric": metric, "order": order, "eligibility": {"min_balls": balls, "min_dismissals": other}, "limit": limit, "leaders": rows}})


@router.get("/leaderboards/bowling", summary="Bowling leaders for a metric, with eligibility and filters", response_model=BowlingLeaderboardResponse, responses=errors())
def bowling_leaders(request: Request, metric: BowlingMetric, limit: Limit = 25, min_balls: Threshold = None, min_wickets: Threshold = None,
                    collection: Collection | None = None, format: Literal["TEST", "ODI", "T20"] | None = None, gender: Literal["male", "female"] | None = None,
                    season: Annotated[str | None, Query(min_length=1, max_length=20)] = None, date_from: date | None = None, date_to: date | None = None):
    strict(request, FILTERS | {"min_wickets"})
    balls, other, rows, order = leaders(request, "bowling", metric, limit, min_balls, min_wickets, (collection, format, gender, season, date_from, date_to))
    return respond(request, BowlingLeaderboardResponse, {"data": {"metric": metric, "order": order, "eligibility": {"min_balls": balls, "min_wickets": other}, "limit": limit, "leaders": rows}})
