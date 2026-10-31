"""A match scorecard, built from the balls, the wickets and the squads.

The cricket rules are written here once:

  Balls faced: every ball the batter faces except a wide. A no ball counts, so do byes and leg byes.
  A four or a six: runs off the bat of 4 or 6 that Cricsheet does not mark as run (non_boundary).
  Runs against a bowler: runs off the bat, wides and no balls. Byes, leg byes and penalty runs are not charged to the bowler.
  Balls bowled: legal deliveries only, so a wide or a no ball is not a ball of the over.
  actual_overs: the legal deliveries bowled, written as completed overs and balls (146.1 is 146 overs and 1 ball). Run rates and economy use these balls.
  scorecard_overs: the overs as the umpires counted them, from the over boundaries Cricsheet records. See official_overs.
  A maiden: a confirmed complete over, bowled whole by one bowler, with no run charged to them. See maidens.py.
  Wickets credited to a bowler: bowled, caught, caught and bowled, lbw, stumped and hit wicket. A run out or a retirement is not.
  A wicket of the team: any dismissal except retired hurt and retired not out.
  Innings total: runs off the bat and extras on the balls, plus penalty runs awarded before or after the innings.
"""
import json

from cricket_api.errors import ApiError
from cricket_api.maidens import MAIDEN_OVERS

CREDITED = {"bowled", "caught", "caught and bowled", "lbw", "stumped", "hit wicket"}
NOT_A_WICKET = {"retired hurt", "retired not out"}


def overs_text(legal_balls: int, per_over: int) -> str:
    return f"{legal_balls // per_over}.{legal_balls % per_over}"


def official_overs(db, match_id: int, per_over: int) -> tuple[dict, dict]:
    """The overs as the umpires counted them: (innings -> text, (innings, bowler) -> text). A value is None when the source cannot settle it.

    An over is one over when it has the full number of legal balls, or when Cricsheet lists it as miscounted (the umpires called it an
    over although it held 7 balls, or 5). Only the final over of an innings may be short, and then its balls count as part of an over. A bowler is credited
    with the balls he bowled in each over, except a miscounted over he bowled whole, which is a full over to him whatever its real length. The answer is None for
    an innings with any other irregular over (short or long, not last, not listed), because there is no way to tell what the umpires counted. It is None for a bowler
    who bowled part of such an over or part of a miscounted over, for the same reason.
    """
    rows = db.query("SELECT innings, over, bowler, count(*) FILTER (WHERE is_legal_delivery) AS legal FROM balls WHERE match_id = ? GROUP BY innings, over, bowler", [match_id])
    miscounted = {(m["innings"], m["over"]): m["balls"] for m in db.query("SELECT innings, over, balls FROM miscounted_overs WHERE match_id = ?", [match_id])}
    overs: dict[tuple, dict] = {}
    for r in rows:
        overs.setdefault((r["innings"], r["over"]), {})[r["bowler"]] = r["legal"]
    last = {}
    for innings, over in overs:
        last[innings] = max(last.get(innings, over), over)
    complete, partial, broken = {}, {}, set()
    credit: dict[tuple, int | None] = {}

    def add(key, balls):
        credit[key] = None if credit.get(key, 0) is None or balls is None else credit.get(key, 0) + balls

    for (innings, over) in sorted(overs):
        by_bowler, total = overs[(innings, over)], sum(overs[(innings, over)].values())
        if (innings, over) in miscounted and total == miscounted[(innings, over)]:
            complete[innings] = complete.get(innings, 0) + 1
            for bowler, legal in by_bowler.items():
                add((innings, bowler), per_over if legal == total else None)
        elif (innings, over) not in miscounted and total == per_over:
            complete[innings] = complete.get(innings, 0) + 1
            for bowler, legal in by_bowler.items():
                add((innings, bowler), legal)
        elif (innings, over) not in miscounted and over == last[innings] and total < per_over:
            partial[innings] = total
            for bowler, legal in by_bowler.items():
                add((innings, bowler), legal)
        else:
            broken.add(innings)
            for bowler in by_bowler:
                add((innings, bowler), None)
    by_innings = {i: (None if i in broken else f"{complete.get(i, 0)}.{partial.get(i, 0)}") for i in last}
    by_bowler = {key: (None if balls is None else overs_text(balls, per_over)) for key, balls in credit.items()}
    return by_innings, by_bowler


def rate(amount: float, balls: int, per_over: int, scale: float = 1.0) -> float | None:
    """A rate per over (balls / per_over) or per hundred balls. None when there were no balls."""
    return round(amount * scale / balls, 2) if balls else None


def fielder_text(fielders: list[dict]) -> str:
    names = [("sub (" + f["fielder"] + ")" if f["substitute"] and f["fielder"] else "sub" if f["substitute"] else f["fielder"]) for f in fielders if f["fielder"] or f["substitute"]]
    return "/".join(names)


def dismissal_text(kind: str, bowler: str | None, fielders: list[dict]) -> str:
    """The way a scorecard writes how a batter was out."""
    catcher = fielder_text(fielders[:1])
    if kind == "caught":
        return f"c {catcher or 'sub'} b {bowler}"
    if kind == "caught and bowled":
        return f"c & b {bowler}"
    if kind == "bowled":
        return f"b {bowler}"
    if kind == "lbw":
        return f"lbw b {bowler}"
    if kind == "stumped":
        return f"st {catcher or 'sub'} b {bowler}"
    if kind == "hit wicket":
        return f"hit wicket b {bowler}"
    if kind == "run out":
        return f"run out ({fielder_text(fielders)})" if fielders else "run out"
    return kind


def build(db, match: dict, totals: list[dict]) -> dict:
    match_id, per_over = match["match_id"], match["balls_per_over"] or 6
    q = db.query
    batting = q("SELECT innings, striker AS name, sum(coalesce(runs_off_bat, 0)) AS runs, count(*) FILTER (WHERE coalesce(wides, 0) = 0) AS balls, "
                "count(*) FILTER (WHERE runs_off_bat = 4 AND NOT non_boundary) AS fours, count(*) FILTER (WHERE runs_off_bat = 6 AND NOT non_boundary) AS sixes, "
                "any_value(striker_id) AS player_id FROM balls WHERE match_id = ? GROUP BY innings, striker", [match_id])
    seen = q("SELECT innings, name, min(slot) AS first_seen FROM (SELECT innings, striker AS name, delivery_number * 2 AS slot FROM balls WHERE match_id = ? "
             "UNION ALL SELECT innings, non_striker, delivery_number * 2 + 1 FROM balls WHERE match_id = ?) GROUP BY innings, name ORDER BY innings, first_seen", [match_id, match_id])
    out = q("SELECT w.innings, w.delivery_number, w.wicket_number, w.player_dismissed AS name, w.player_dismissed_id AS player_id, w.wicket_type, b.bowler, b.bowler_id, "
            "b.scorecard_ball, f.fielders FROM wickets w LEFT JOIN balls b ON b.match_id = w.match_id AND b.innings = w.innings AND b.delivery_number = w.delivery_number "
            "LEFT JOIN (SELECT innings, delivery_number, wicket_number, list({'fielder': fielder, 'substitute': substitute} ORDER BY fielder_number) AS fielders "
            "FROM wicket_fielders WHERE match_id = ? GROUP BY innings, delivery_number, wicket_number) f "
            "ON f.innings = w.innings AND f.delivery_number = w.delivery_number AND f.wicket_number = w.wicket_number "
            "WHERE w.match_id = ? ORDER BY w.innings, w.delivery_number, w.wicket_number", [match_id, match_id])
    scores = q("SELECT innings, delivery_number, sum(coalesce(runs_off_bat, 0) + coalesce(extras, 0)) OVER (PARTITION BY innings ORDER BY delivery_number) AS score "
               "FROM balls WHERE match_id = ?", [match_id])
    bowling = q("SELECT innings, bowler AS name, any_value(bowler_id) AS player_id, min(delivery_number) AS first_ball, count(*) FILTER (WHERE is_legal_delivery) AS legal, "
                "sum(coalesce(runs_off_bat, 0)) + sum(coalesce(wides, 0)) + sum(coalesce(noballs, 0)) AS conceded, sum(coalesce(wides, 0)) AS wides, "
                "sum(coalesce(noballs, 0)) AS noballs FROM balls WHERE match_id = ? GROUP BY innings, bowler", [match_id])
    maidens = q("SELECT innings, bowler AS name, count(*) AS maidens FROM (" + MAIDEN_OVERS.format(where="WHERE b.match_id = ?") + ") GROUP BY innings, bowler", [match_id])
    extras = q("SELECT innings, sum(coalesce(byes, 0)) AS byes, sum(coalesce(legbyes, 0)) AS legbyes, sum(coalesce(wides, 0)) AS wides, sum(coalesce(noballs, 0)) AS noballs, "
               "sum(coalesce(penalty, 0)) AS penalty, any_value(bowling_team) AS bowling_team FROM balls WHERE match_id = ? GROUP BY innings", [match_id])
    squads = q("SELECT team, person AS name, person_id FROM match_people WHERE match_id = ? AND role = 'player' ORDER BY person_number", [match_id])

    ids = {s["name"]: s["person_id"] for s in squads}
    official_innings, official_bowlers = official_overs(db, match_id, per_over)
    absent = q("SELECT innings, player, player_id FROM innings_absent_hurt WHERE match_id = ? ORDER BY innings, player", [match_id])
    score_at = {(s["innings"], s["delivery_number"]): s["score"] for s in scores}
    innings_list = []
    for total in totals:
        number = total["innings"]
        stats = {b["name"]: b for b in batting if b["innings"] == number}
        dismissed = {o["name"]: o for o in out if o["innings"] == number}
        card = []
        appearances = [s for s in seen if s["innings"] == number]
        present = {a["name"] for a in appearances}
        for o in out:           # a batter out without facing a ball, such as several retired out on one delivery, is on the card and not among those who did not bat
            if o["innings"] == number and o["name"] and o["name"] not in present:
                appearances.append({"name": o["name"]})
                present.add(o["name"])
        for order, appearance in enumerate(appearances, start=1):
            name, s, o = appearance["name"], stats.get(appearance["name"]), dismissed.get(appearance["name"])
            line = {"order": order, "player": name, "player_id": (s or {}).get("player_id") or ids.get(name), "runs": int((s or {}).get("runs") or 0), "balls": int((s or {}).get("balls") or 0),
                    "fours": int((s or {}).get("fours") or 0), "sixes": int((s or {}).get("sixes") or 0)}
            line["strike_rate"] = rate(line["runs"], line["balls"], per_over, 100)
            if o:
                fielders = o["fielders"] or []
                credited = o["wicket_type"] in CREDITED
                line["dismissal"] = {"kind": o["wicket_type"], "bowler": o["bowler"] if credited else None, "bowler_id": o["bowler_id"] if credited else None,
                                     "fielders": [f["fielder"] for f in fielders if f["fielder"]], "text": dismissal_text(o["wicket_type"], o["bowler"], fielders)}
            else:
                line["dismissal"] = None
            card.append(line)
        batters = {c["player"] for c in card}
        absent_hurt = [{"player": a["player"], "player_id": a["player_id"]} for a in absent if a["innings"] == number and a["player"] not in batters]
        away = {a["player"] for a in absent_hurt}
        did_not_bat = [{"player": s["name"], "player_id": s["person_id"]} for s in squads if s["team"] == total["batting_team"] and s["name"] not in batters | away]
        fall, count = [], 0
        for o in [o for o in out if o["innings"] == number and o["wicket_type"] not in NOT_A_WICKET]:
            count += 1
            fall.append({"wicket": count, "score": int(score_at.get((number, o["delivery_number"]), 0)) + total["penalty_runs_pre"], "player": o["name"], "ball": o["scorecard_ball"]})
        mine = sorted([b for b in bowling if b["innings"] == number], key=lambda b: b["first_ball"])
        maiden_count = {m["name"]: m["maidens"] for m in maidens if m["innings"] == number}
        credited_count: dict[str, int] = {}
        for o in out:
            if o["innings"] == number and o["wicket_type"] in CREDITED and o["bowler"]:
                credited_count[o["bowler"]] = credited_count.get(o["bowler"], 0) + 1
        figures = [{"order": i, "bowler": b["name"], "player_id": b["player_id"] or ids.get(b["name"]), "actual_overs": overs_text(int(b["legal"]), per_over), "scorecard_overs": official_bowlers.get((number, b["name"])), "maidens": int(maiden_count.get(b["name"], 0)),
                    "runs": int(b["conceded"]), "wickets": credited_count.get(b["name"], 0), "wides": int(b["wides"]), "noballs": int(b["noballs"]),
                    "economy": rate(int(b["conceded"]), int(b["legal"]), per_over, per_over)} for i, b in enumerate(mine, start=1)]
        e = next((x for x in extras if x["innings"] == number), {"byes": 0, "legbyes": 0, "wides": 0, "noballs": 0, "penalty": 0, "bowling_team": None})
        penalty = int(e["penalty"]) + total["penalty_runs_pre"] + total["penalty_runs_post"]
        extras_total = int(e["byes"]) + int(e["legbyes"]) + int(e["wides"]) + int(e["noballs"]) + penalty
        innings_list.append({
            "innings": number, "batting_team": total["batting_team"], "bowling_team": e["bowling_team"], "is_super_over": total["is_super_over"], "declared": total["declared"],
            "total": {"runs": total["runs"], "wickets": total["wickets"], "actual_overs": total["actual_overs"], "scorecard_overs": total["scorecard_overs"], "legal_balls": total["legal_balls"],
                      "run_rate": rate(total["runs"], total["legal_balls"], per_over, per_over)},
            "extras": {"byes": int(e["byes"]), "legbyes": int(e["legbyes"]), "wides": int(e["wides"]), "noballs": int(e["noballs"]), "penalty": penalty, "total": extras_total},
            "batting": card, "did_not_bat": did_not_bat, "absent_hurt": absent_hurt, "fall_of_wickets": fall, "bowling": figures})
    if not innings_list:
        raise ApiError(404, "This match has no scorecard")
    return {"match_id": match_id, "balls_per_over": per_over, "innings": innings_list}


def innings_totals(db, match_id: int, per_over: int) -> list[dict]:
    rows = db.query(
        "WITH t AS (SELECT innings, sum(coalesce(runs_off_bat, 0) + coalesce(extras, 0)) AS ball_runs, count(*) FILTER (WHERE is_legal_delivery) AS legal_balls, "
        "count(*) AS deliveries FROM balls WHERE match_id = ? GROUP BY innings), "
        "w AS (SELECT innings, count(*) FILTER (WHERE wicket_type NOT IN ('retired hurt', 'retired not out')) AS wickets FROM wickets WHERE match_id = ? GROUP BY innings) "
        "SELECT i.innings, i.batting_team, i.target_runs, i.target_overs, i.is_super_over, i.declared, i.penalty_runs_pre, i.penalty_runs_post, "
        "coalesce(t.ball_runs, 0) + i.penalty_runs_pre + i.penalty_runs_post AS runs, coalesce(w.wickets, 0) AS wickets, coalesce(t.legal_balls, 0) AS legal_balls, "
        "coalesce(t.deliveries, 0) AS deliveries FROM innings i LEFT JOIN t ON t.innings = i.innings LEFT JOIN w ON w.innings = i.innings "
        "WHERE i.match_id = ? ORDER BY i.innings", [match_id, match_id, match_id])
    official, _ = official_overs(db, match_id, per_over)
    for row in rows:
        row["actual_overs"] = overs_text(row["legal_balls"], per_over)
        row["scorecard_overs"] = official.get(row["innings"])
    return rows


def stored(db, match_id: int) -> dict:
    """The scorecard data of a match as the export stored it (match_scorecards): the same data build() returns, written once when the data is made. A match with no innings has a NULL there and the same 404 as build().
    A match with no row at all is a fault, not a 404: the export refuses data in which a match lacks one."""
    rows = db.query("SELECT scorecard FROM match_scorecards WHERE match_id = ?", [match_id])
    if not rows:
        raise RuntimeError(f"match {match_id} has no row in match_scorecards")
    if rows[0]["scorecard"] is None:
        raise ApiError(404, "This match has no scorecard")
    return json.loads(rows[0]["scorecard"])


def build_for_match(db, match_id: int) -> dict:
    """The scorecard data built from the balls, the wickets and the squads (the definition that match_scorecards stores). Raises ApiError(404) when the match has no innings."""
    match = db.query("SELECT * FROM matches WHERE match_id = ?", [match_id])[0]
    return build(db, match, innings_totals(db, match_id, match["balls_per_over"] or 6))

