"""The parts of a match that sit beside its scorecard: powerplays, reviews, replacements, wickets and notes.

A match that exists but has none of a part answers 200 with an empty collection. Only a match that does not exist is a 404. Every list keeps
Cricsheet's order and carries the ordinal Cricsheet's own order gives it, so nothing depends on how a client sorts. The absent hurt
list is the exception: the data has no source order for it, so it is sorted by innings and player name, which is only a presentation order.
"""
from fastapi import APIRouter, Request

from cricket_api.http import respond, strict
from cricket_api.models import NotesResponse, PowerplaysResponse, ReplacementsResponse, ReviewsResponse, WicketsResponse
from cricket_api.openapi import errors
from cricket_api.routes.matches import MatchId, fetch_match
from cricket_api.scorecard import CREDITED

router = APIRouter(prefix="/v1")


def rows(request: Request, match_id: int, sql: str) -> list[dict]:
    fetch_match(request, match_id)
    return request.app.state.db.query(sql, [match_id])


@router.get("/matches/{match_id}/powerplays", summary="The powerplays of every innings, with their boundaries as an over, a position and a delivery number",
            response_model=PowerplaysResponse, responses=errors(404))
def powerplays(request: Request, match_id: MatchId):
    strict(request, set())
    found = rows(request, match_id, "SELECT innings, powerplay_number, type, start_over, start_ball_in_over, start_delivery_number, start_listed, end_over, end_ball_in_over, "
                                    "end_delivery_number, end_listed FROM powerplays WHERE match_id = ? ORDER BY innings, powerplay_number")
    data = [{"innings": r["innings"], "powerplay_number": r["powerplay_number"], "type": r["type"],
             "start": {"over": r["start_over"], "ball_in_over": r["start_ball_in_over"], "delivery_number": r["start_delivery_number"], "listed": r["start_listed"]},
             "end": {"over": r["end_over"], "ball_in_over": r["end_ball_in_over"], "delivery_number": r["end_delivery_number"], "listed": r["end_listed"]}} for r in found]
    return respond(request, PowerplaysResponse, {"data": data})


@router.get("/matches/{match_id}/reviews", summary="The decision reviews of a match, in the order they were made", response_model=ReviewsResponse, responses=errors(404))
def reviews(request: Request, match_id: MatchId):
    strict(request, set())
    found = rows(request, match_id,
                 "SELECT row_number() OVER (ORDER BY r.innings, r.delivery_number) AS review_number, r.innings, r.delivery_number, b.over, b.ball_in_over, r.review_by, r.umpire, "
                 "r.umpire_id, r.batter, r.batter_id, r.decision, r.review_type, r.umpires_call FROM reviews r LEFT JOIN balls b ON b.match_id = r.match_id AND b.innings = r.innings "
                 "AND b.delivery_number = r.delivery_number WHERE r.match_id = ? ORDER BY r.innings, r.delivery_number")
    return respond(request, ReviewsResponse, {"data": found})


@router.get("/matches/{match_id}/replacements", summary="Players replaced during a match, in order", response_model=ReplacementsResponse, responses=errors(404))
def replacements(request: Request, match_id: MatchId):
    strict(request, set())
    found = rows(request, match_id,
                 "SELECT r.innings, r.delivery_number, r.replacement_number, b.over, b.ball_in_over, r.kind, r.team, r.role, r.player_in, r.player_in_id, r.player_out, r.player_out_id, "
                 "r.reason FROM match_replacements r LEFT JOIN balls b ON b.match_id = r.match_id AND b.innings = r.innings AND b.delivery_number = r.delivery_number "
                 "WHERE r.match_id = ? ORDER BY r.innings, r.delivery_number, r.replacement_number")
    return respond(request, ReplacementsResponse, {"data": found})


@router.get("/matches/{match_id}/wickets", summary="Every wicket of a match with its fielders, retirements included", response_model=WicketsResponse, responses=errors(404))
def wickets(request: Request, match_id: MatchId):
    strict(request, set())
    found = rows(request, match_id,
                 "SELECT row_number() OVER (PARTITION BY w.innings ORDER BY w.delivery_number, w.wicket_number) AS sequence, w.innings, w.delivery_number, w.wicket_number, b.over, "
                 "b.ball_in_over, w.player_dismissed, w.player_dismissed_id, w.wicket_type, b.bowler, b.bowler_id FROM wickets w LEFT JOIN balls b ON b.match_id = w.match_id "
                 "AND b.innings = w.innings AND b.delivery_number = w.delivery_number WHERE w.match_id = ? ORDER BY w.innings, w.delivery_number, w.wicket_number")
    fielders: dict[tuple, list] = {}
    for f in request.app.state.db.query("SELECT innings, delivery_number, wicket_number, fielder_number, fielder, fielder_id, substitute FROM wicket_fielders WHERE match_id = ? "
                                        "ORDER BY innings, delivery_number, wicket_number, fielder_number", [match_id]):
        fielders.setdefault((f["innings"], f["delivery_number"], f["wicket_number"]), []).append(
            {"fielder_number": f["fielder_number"], "fielder": f["fielder"], "fielder_id": f["fielder_id"], "substitute": f["substitute"]})
    data = [{**w, "credited_to_bowler": w["wicket_type"] in CREDITED, "fielders": fielders.get((w["innings"], w["delivery_number"], w["wicket_number"]), [])} for w in found]
    return respond(request, WicketsResponse, {"data": data})


@router.get("/matches/{match_id}/notes", summary="What the source says about gaps and oddities: missing data, miscounted overs, absent hurt and the match dates",
            response_model=NotesResponse, responses=errors(404))
def notes(request: Request, match_id: MatchId):
    strict(request, set())
    fetch_match(request, match_id)
    q = request.app.state.db.query
    data = {
        "missing_data": q("SELECT missing_number, missing_type, innings, powerplay_type FROM match_missing WHERE match_id = ? ORDER BY missing_number", [match_id]),
        "miscounted_overs": q("SELECT innings, over, balls, umpire, umpire_id FROM miscounted_overs WHERE match_id = ? ORDER BY innings, over", [match_id]),
        "absent_hurt": q("SELECT innings, player, player_id FROM innings_absent_hurt WHERE match_id = ? ORDER BY innings, player", [match_id]),
        "dates": q("SELECT date_number, date FROM match_dates WHERE match_id = ? ORDER BY date_number", [match_id])}
    return respond(request, NotesResponse, {"data": data})
