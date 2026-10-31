"""Read what Cricsheet's JSON files hold that its CSV files leave out.

The ball-by-ball CSV carries no fielders, squads, officials, reviews, powerplays or targets, and it can record only two
wickets on a ball. The JSON file of the same match has all of it. This module turns one JSON match into rows for:

  wickets, wicket_fielders   every wicket of a delivery, and the fielders of each
  match_people               the players of each side and the officials, in source order
  innings, powerplays, reviews, replacements
  match facts                teams, city, last day, length, numbering, collection, result details and the first officials
  fielder columns            the first listed fielder of the first wicket, for the ball rows

Deliveries are counted the way the ball file counts them: the n-th delivery of an innings, in the order Cricsheet lists them,
wides and no balls included. `check_against_rows` proves the JSON and the CSV describe the same deliveries before anything is used.

Cricsheet's CSV files write a person's unique register name, with a suffix such as " (2)" when two people share a name. Its JSON
files write the plain name with the same id. Names are therefore looked up through the id and written the way the CSV spells them.
"""
import json
import re

import pandas as pd

FIRST_FIELDER_COLUMNS = ["fielder_1", "fielder_1_id", "fielder_1_substitute"]
SUFFIX = re.compile(r"\s*\(\d+\)$")

# column on the match row, Cricsheet's list, position in that list
OFFICIAL_SLOTS = [("umpire_1", "umpires", 0), ("umpire_2", "umpires", 1), ("tv_umpire", "tv_umpires", 0), ("match_referee", "match_referees", 0)]
MATCH_FACT_COLUMNS = [
    "team_1", "team_2", "city", "end_date", "scheduled_overs", "balls_per_over", "match_type_number", "collection",
    "result_method", "eliminator", *[slot for slot, _, _ in OFFICIAL_SLOTS],
    "won_by_innings", "cricsheet_created", "cricsheet_revision", "cricsheet_data_version", "event_group", "event_sub_name", "event_stage",
]
PEOPLE_ROLES = {"umpires": "umpire", "tv_umpires": "tv_umpire", "reserve_umpires": "reserve_umpire", "match_referees": "match_referee"}

WICKET_COLUMNS = ["match_id", "innings", "delivery_number", "wicket_number", "player_dismissed", "player_dismissed_id", "wicket_type"]
FIELDER_COLUMNS = ["match_id", "innings", "delivery_number", "wicket_number", "fielder_number", "fielder", "fielder_id", "substitute"]
PEOPLE_COLUMNS = ["match_id", "role", "person_number", "team", "person", "person_id"]
NOTE_COLUMNS = ["match_id", "collection", "note"]
INNINGS_COLUMNS = ["match_id", "innings", "batting_team", "target_runs", "target_overs", "is_super_over", "declared", "penalty_runs_pre", "penalty_runs_post"]
POWERPLAY_COLUMNS = ["match_id", "innings", "powerplay_number", "type", "start_over", "start_ball_in_over", "start_delivery_number", "end_over", "end_ball_in_over",
                     "end_delivery_number", "start_listed", "end_listed"]
REVIEW_COLUMNS = ["match_id", "innings", "delivery_number", "review_by", "umpire", "umpire_id", "batter", "batter_id", "decision", "review_type", "umpires_call"]
REPLACEMENT_COLUMNS = ["match_id", "innings", "delivery_number", "replacement_number", "kind", "team", "role", "player_in", "player_in_id", "player_out", "player_out_id", "reason"]
MISSING_COLUMNS = ["match_id", "missing_number", "missing_type", "innings", "powerplay_type"]
MISCOUNTED_COLUMNS = ["match_id", "innings", "over", "balls", "umpire", "umpire_id"]
ABSENT_HURT_COLUMNS = ["match_id", "innings", "player", "player_id"]
DATES_COLUMNS = ["match_id", "date_number", "date"]
TABLE_COLUMNS = {
    "facts": ["match_id", *MATCH_FACT_COLUMNS], "wickets": WICKET_COLUMNS, "wicket_fielders": FIELDER_COLUMNS, "people": PEOPLE_COLUMNS,
    "innings": INNINGS_COLUMNS, "powerplays": POWERPLAY_COLUMNS, "reviews": REVIEW_COLUMNS, "replacements": REPLACEMENT_COLUMNS,
    "missing": MISSING_COLUMNS, "miscounted_overs": MISCOUNTED_COLUMNS, "absent_hurt": ABSENT_HURT_COLUMNS, "dates": DATES_COLUMNS,
}

# Keys Cricsheet's JSON may carry. One outside these lists is reported, so a new field is noticed and not dropped unseen.
KNOWN_INNINGS_KEYS = {"team", "overs", "declared", "penalty_runs", "miscounted_overs", "absent_hurt", "powerplays", "target", "super_over"}
KNOWN_INFO_KEYS = {"balls_per_over", "bowl_out", "city", "dates", "event", "gender", "match_type", "match_type_number", "missing", "officials", "outcome", "overs",
                   "player_of_match", "players", "registry", "season", "supersubs", "team_type", "teams", "toss", "venue"}
KNOWN_NESTED_KEYS = {"event": {"name", "match_number", "stage", "group", "sub_name"}, "outcome": {"by", "winner", "method", "eliminator", "result", "bowl_out"},
                     "toss": {"decision", "winner", "uncontested"}, "officials": {"umpires", "tv_umpires", "match_referees", "reserve_umpires"}}
KNOWN_DELIVERY_KEYS = {"actual_delivery", "batter", "bowler", "non_striker", "runs", "extras", "wickets", "review", "replacements"}
# Present in the source and deliberately not stored: how often is reported in the review file
UNSUPPORTED = {"bowl_out": "bowl out attempts", "supersubs": "supersub lists", "toss.uncontested": "an uncontested toss"}


def load(raw: bytes | str) -> dict:
    return json.loads(raw)


def people(match: dict) -> dict[str, str]:
    """Name to Cricsheet identifier, for players, fielders and officials alike."""
    return match["info"].get("registry", {}).get("people", {})


class Names:
    """Turns a JSON name into the spelling the ball file uses, by way of the id."""

    def __init__(self, match: dict, csv_registry: dict[str, str] | None = None):
        self.ids = people(match)
        self.spelling = {identifier: name for name, identifier in (csv_registry or {}).items()}

    def id(self, name: str | None) -> str | None:
        return self.ids.get(name) if name else None

    def name(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.spelling.get(self.ids.get(name), name)


def deliveries(match: dict):
    """Every delivery as (innings number, delivery number, the delivery), in the order of the ball file."""
    for innings_number, innings in enumerate(match["innings"], start=1):
        number = 0
        for over in innings.get("overs", []):
            for delivery in over["deliveries"]:
                number += 1
                yield innings_number, number, delivery


def unexpected_keys(match: dict) -> list[str]:
    """Keys in the JSON that this module does not read or deliberately skip, as paths."""
    info = match["info"]
    found = {f"info.{k}" for k in set(info) - KNOWN_INFO_KEYS}
    for parent, allowed in KNOWN_NESTED_KEYS.items():
        if isinstance(info.get(parent), dict):
            found |= {f"info.{parent}.{k}" for k in set(info[parent]) - allowed}
    found |= {f"innings.{k}" for innings in match["innings"] for k in set(innings) - KNOWN_INNINGS_KEYS}
    found |= {f"delivery.{k}" for _, _, delivery in deliveries(match) for k in set(delivery) - KNOWN_DELIVERY_KEYS}
    return sorted(found)


def unsupported_notes(match: dict, match_id: int, collection: str) -> list[dict]:
    info, found = match["info"], []
    for path, text in UNSUPPORTED.items():
        parent, _, child = path.partition(".")
        present = (child in info.get(parent, {})) if child else (parent in info)
        if present:
            found.append({"match_id": match_id, "collection": collection, "note": f"{text} are in the JSON and not stored"})
    return found


def _plain(name) -> str:
    return SUFFIX.sub("", str(name))


def _differs(json_names, csv_names, json_ids: dict, csv_ids: dict) -> tuple[int, int]:
    """(hard, soft) counts of pairs that do not name the same person.

    Hard: both sides have an id and the ids differ. That is a different person. Soft: the spellings differ and an id is missing on a side,
    which happens when Cricsheet's two formats spell a name differently and the CSV register does not know it. Soft differences are
    reported and do not stop the match.
    """
    hard = soft = 0
    for left, right in zip(json_names, csv_names):
        a, b = json_ids.get(left), csv_ids.get(right)
        if a and b:
            hard += a != b
        elif _plain(left) != _plain(right):
            soft += 1
    return int(hard), int(soft)


def check_against_rows(match: dict, rows: pd.DataFrame, csv_registry: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    """Compare the JSON with the CSV rows of the same match. Returns (problems, notes). A problem means the two do not describe the same match."""
    problems, notes, json_ids, csv_ids = [], [], people(match), csv_registry or {}
    if len(match["innings"]) != rows["innings"].nunique():
        return [f"{len(match['innings'])} innings in the JSON, {rows['innings'].nunique()} in the CSV"], notes
    expected = pd.DataFrame(
        [(i, n, d["batter"], d["bowler"], d["non_striker"], d["runs"]["batter"], d["runs"]["extras"]) for i, n, d in deliveries(match)],
        columns=["innings", "delivery_number", "striker", "bowler", "non_striker", "runs_off_bat", "extras"])
    actual = rows[["innings", "striker", "bowler", "non_striker", "runs_off_bat", "extras"]].copy()
    actual["delivery_number"] = actual.groupby("innings").cumcount() + 1
    actual = actual[expected.columns]
    if len(expected) != len(actual):
        return [f"{len(expected)} deliveries in the JSON, {len(actual)} in the CSV"], notes
    for column in ("striker", "bowler", "non_striker"):
        hard, soft = _differs(expected[column], actual[column], json_ids, csv_ids)
        if hard:
            problems.append(f"{hard} deliveries have a different {column}")
        if soft:
            notes.append(f"{soft} deliveries spell the {column} differently in the JSON and the CSV")
    for column in ("runs_off_bat", "extras"):
        bad = int((expected[column].to_numpy() != pd.to_numeric(actual[column]).fillna(0).to_numpy()).sum())
        if bad:
            problems.append(f"{bad} deliveries have different {column}")
    wicket_problems, wicket_notes = _wicket_problems(match, rows, json_ids, csv_ids)
    return problems + wicket_problems, notes + wicket_notes


def _wicket_problems(match: dict, rows: pd.DataFrame, json_ids: dict, csv_ids: dict) -> tuple[list[str], list[str]]:
    """Wicket 1 and 2 of the JSON must be the ball file's first and second dismissal. A different kind or a different person is a problem."""
    ordered = rows.assign(delivery_number=rows.groupby("innings").cumcount() + 1).set_index(["innings", "delivery_number"])
    wrong = spelling = 0
    for innings, number, delivery in deliveries(match):
        wickets = delivery.get("wickets", [])
        if not wickets:
            continue
        row = ordered.loc[(innings, number)]
        pairs = [(wickets[0], "player_dismissed", "wicket_type")] + ([(wickets[1], "other_player_dismissed", "other_wicket_type")] if len(wickets) > 1 else [])
        for wicket, who, kind in pairs:
            hard, soft = _differs([wicket["player_out"]], [row[who]], json_ids, csv_ids)
            wrong += bool(hard) or wicket["kind"] != row[kind]
            spelling += soft
    return ([f"{wrong} wickets differ between the JSON and the ball rows"] if wrong else []), \
           ([f"{spelling} dismissed players are spelled differently in the JSON and the CSV"] if spelling else [])


def facts(match: dict, collection: str, names: Names) -> dict:
    info = match["info"]
    teams, outcome = info.get("teams", []), info.get("outcome", {})
    row = {
        "team_1": teams[0] if len(teams) > 0 else None, "team_2": teams[1] if len(teams) > 1 else None,
        "city": info.get("city"), "end_date": max(info["dates"]) if info.get("dates") else None,
        "scheduled_overs": info.get("overs"), "balls_per_over": info.get("balls_per_over"),
        "match_type_number": info.get("match_type_number"), "collection": collection,
        "result_method": outcome.get("method"), "eliminator": outcome.get("eliminator") or outcome.get("bowl_out"),
    }
    event, meta = info.get("event", {}), match.get("meta", {})
    group = event.get("group")
    row.update({"won_by_innings": bool(outcome.get("by", {}).get("innings")), "cricsheet_created": meta.get("created"), "cricsheet_revision": meta.get("revision"),
                "cricsheet_data_version": meta.get("data_version"), "event_group": None if group is None else str(group),
                "event_sub_name": event.get("sub_name"), "event_stage": event.get("stage")})
    officials = info.get("officials", {})
    for slot, key, position in OFFICIAL_SLOTS:
        listed = officials.get(key, [])
        row[slot] = names.name(listed[position]) if position < len(listed) else None
    return row


def wicket_rows(match: dict, match_id: int, names: Names) -> tuple[list[dict], list[dict]]:
    """Every wicket, and every fielder of every wicket. A wicket's number is its position in Cricsheet's list for the delivery."""
    wickets, fielders = [], []
    for innings, number, delivery in deliveries(match):
        for wicket_number, wicket in enumerate(delivery.get("wickets", []), start=1):
            key = {"match_id": match_id, "innings": innings, "delivery_number": number, "wicket_number": wicket_number}
            wickets.append({**key, "player_dismissed": names.name(wicket["player_out"]), "player_dismissed_id": names.id(wicket["player_out"]),
                            "wicket_type": wicket["kind"]})
            for fielder_number, fielder in enumerate(wicket.get("fielders", []), start=1):
                # Cricsheet sometimes records only that a substitute fielded, with no name
                fielders.append({**key, "fielder_number": fielder_number, "fielder": names.name(fielder.get("name")), "fielder_id": names.id(fielder.get("name")),
                                 "substitute": bool(fielder.get("substitute", False))})
    return wickets, fielders


def spell_like_the_ball_rows(wickets: list[dict], rows: pd.DataFrame) -> list[dict]:
    """Wicket 1 and 2 are the ball rows' first and second dismissal, so their names are taken from those rows.

    This keeps wickets.csv spelled exactly like the ball file even where Cricsheet's JSON spells the player another way.
    """
    ordered = rows.assign(delivery_number=rows.groupby("innings").cumcount() + 1).set_index(["innings", "delivery_number"])
    fixed = []
    for wicket in wickets:
        column = {1: "player_dismissed", 2: "other_player_dismissed"}.get(wicket["wicket_number"])
        name = ordered.loc[(wicket["innings"], wicket["delivery_number"]), column] if column else None
        fixed.append({**wicket, "player_dismissed": name} if column and isinstance(name, str) else wicket)
    return fixed


def first_fielders(fielders: list[dict]) -> dict[tuple[int, int], dict]:
    """The ball file's fielder columns: the first listed fielder of the first wicket, by (innings, delivery number)."""
    found = {}
    for row in fielders:
        if row["wicket_number"] == 1 and row["fielder_number"] == 1:
            found[(row["innings"], row["delivery_number"])] = {
                "fielder_1": row["fielder"], "fielder_1_id": row["fielder_id"], "fielder_1_substitute": row["substitute"]}
    return found


def people_rows(match: dict, match_id: int, names: Names) -> list[dict]:
    rows, number = [], 0
    for team, players in match["info"].get("players", {}).items():
        for name in players:
            number += 1
            rows.append({"match_id": match_id, "role": "player", "person_number": number, "team": team, "person": names.name(name), "person_id": names.id(name)})
    for key, role in PEOPLE_ROLES.items():
        for number, name in enumerate(match["info"].get("officials", {}).get(key, []), start=1):
            rows.append({"match_id": match_id, "role": role, "person_number": number, "team": None, "person": names.name(name), "person_id": names.id(name)})
    return rows


def innings_rows(match: dict, match_id: int) -> list[dict]:
    rows = []
    for number, innings in enumerate(match["innings"], start=1):
        target, penalty = innings.get("target", {}), innings.get("penalty_runs", {})
        rows.append({"match_id": match_id, "innings": number, "batting_team": innings["team"], "target_runs": target.get("runs"), "target_overs": target.get("overs"),
                     "is_super_over": bool(innings.get("super_over", False)), "declared": bool(innings.get("declared", False)),
                     "penalty_runs_pre": penalty.get("pre", 0), "penalty_runs_post": penalty.get("post", 0)})
    return rows


def powerplay_rows(match: dict, match_id: int) -> list[dict]:
    """The powerplays as Cricsheet lists them. Its from and to are over.ball written as one number, such as 5.11 for the 11th delivery of over 5, so the
    text is kept as listed and `resolve_powerplays` turns it into an over, a position and a delivery number once the deliveries are known."""
    return [{"match_id": match_id, "innings": number, "powerplay_number": order, "type": p.get("type"), "start_over": None, "start_ball_in_over": None,
             "start_delivery_number": None, "end_over": None, "end_ball_in_over": None, "end_delivery_number": None,
             "start_listed": None if p.get("from") is None else str(p["from"]), "end_listed": None if p.get("to") is None else str(p["to"])}
            for number, innings in enumerate(match["innings"], start=1) for order, p in enumerate(innings.get("powerplays", []), start=1)]


def _position(listed: str | None, deliveries: dict[int, int], side: str) -> tuple[int | None, int | None, int | None]:
    """(over, position in the over, delivery number) for one listed boundary, or Nones where it matches no single delivery.

    Cricsheet writes the position after a point, so 5.11 is the 11th delivery. A position of 10 reads as 1 (5.10 is written 5.1), and so the written
    .1 can be the 1st or the 10th delivery. Where both exist, a start is the first and an end is whichever is the last delivery of the over.
    """
    if listed is None:
        return None, None, None
    over_text, _, fraction = listed.partition(".")
    over = int(over_text)
    position = int(fraction or 0)
    options = [position] + ([10] if len(fraction) == 1 and position == 1 else [])
    present = [p for p in options if (over, p) in deliveries]
    if len(present) > 1:
        present = [1] if side == "start" else [p for p in present if p == max(p for (o, p) in deliveries if o == over)]
    if len(present) != 1:
        return (over, position, None) if len(options) == 1 else (over, None, None)
    return over, present[0], deliveries[(over, present[0])]


def resolve_powerplays(powerplays: list[dict], rows: pd.DataFrame) -> list[dict]:
    """Fill in the over, position and delivery number of each powerplay boundary from the match's deliveries."""
    ordered = rows.assign(delivery_number=rows.groupby("innings").cumcount() + 1)
    by_innings = {}
    for innings, over, position, number in zip(ordered["innings"], ordered["over"], ordered["ball_in_over"], ordered["delivery_number"]):
        by_innings.setdefault(int(innings), {})[(int(over), int(position))] = int(number)
    resolved = []
    for row in powerplays:
        deliveries = by_innings.get(row["innings"], {})
        s_over, s_pos, s_number = _position(row["start_listed"], deliveries, "start")
        e_over, e_pos, e_number = _position(row["end_listed"], deliveries, "end")
        resolved.append({**row, "start_over": s_over, "start_ball_in_over": s_pos, "start_delivery_number": s_number,
                         "end_over": e_over, "end_ball_in_over": e_pos, "end_delivery_number": e_number})
    return resolved


def review_rows(match: dict, match_id: int, names: Names) -> list[dict]:
    rows = []
    for innings, number, delivery in deliveries(match):
        review = delivery.get("review")
        if review:
            rows.append({"match_id": match_id, "innings": innings, "delivery_number": number, "review_by": review.get("by"),
                         "umpire": names.name(review.get("umpire")), "umpire_id": names.id(review.get("umpire")),
                         "batter": names.name(review.get("batter")), "batter_id": names.id(review.get("batter")),
                         "decision": review.get("decision"), "review_type": review.get("type"), "umpires_call": bool(review.get("umpires_call", False))})
    return rows


def replacement_rows(match: dict, match_id: int, names: Names) -> list[dict]:
    rows = []
    for innings, number, delivery in deliveries(match):
        order = 0
        for kind, changes in delivery.get("replacements", {}).items():
            for change in changes:
                order += 1
                rows.append({"match_id": match_id, "innings": innings, "delivery_number": number, "replacement_number": order, "kind": kind,
                             "team": change.get("team"), "role": change.get("role"), "player_in": names.name(change.get("in")), "player_in_id": names.id(change.get("in")),
                             "player_out": names.name(change.get("out")), "player_out_id": names.id(change.get("out")), "reason": change.get("reason")})
    return rows


def missing_rows(match: dict, match_id: int) -> list[dict]:
    """What Cricsheet says it does not have for the match, in its order: a plain item, or powerplays of an innings by type.

    The same powerplay type can be listed twice for an innings when two of them are missing, so every row carries its place in the list.
    """
    found = []
    for item in match["info"].get("missing", []):
        if isinstance(item, str):
            found.append((item, None, None))
            continue
        for kind, detail in item.items():
            if kind != "powerplays":
                raise ValueError(f"unknown kind of missing data: {kind}")
            for innings, types in detail.items():
                found += [("powerplays", int(innings), t) for t in types] or [("powerplays", int(innings), None)]
    return [{"match_id": match_id, "missing_number": number, "missing_type": kind, "innings": innings, "powerplay_type": powerplay}
            for number, (kind, innings, powerplay) in enumerate(found, start=1)]


def miscounted_rows(match: dict, match_id: int, names: Names) -> list[dict]:
    return [{"match_id": match_id, "innings": number, "over": int(over), "balls": int(detail["balls"]), "umpire": names.name(detail.get("umpire")),
             "umpire_id": names.id(detail.get("umpire"))}
            for number, innings in enumerate(match["innings"], start=1) for over, detail in innings.get("miscounted_overs", {}).items()]


def absent_hurt_rows(match: dict, match_id: int, names: Names) -> list[dict]:
    return [{"match_id": match_id, "innings": number, "player": names.name(name), "player_id": names.id(name)}
            for number, innings in enumerate(match["innings"], start=1) for name in innings.get("absent_hurt", [])]


def date_rows(match: dict, match_id: int) -> list[dict]:
    return [{"match_id": match_id, "date_number": number, "date": day} for number, day in enumerate(match["info"].get("dates", []), start=1)]


def extras(match: dict, match_id: int, collection: str, csv_registry: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """Everything one match adds to the ball rows, as plain rows per table. Unexpected keys raise, so a new field is not lost."""
    unexpected = unexpected_keys(match)
    if unexpected:
        raise ValueError(f"unknown keys in the JSON: {unexpected}")
    names = Names(match, csv_registry)
    wickets, fielders = wicket_rows(match, match_id, names)
    return {"facts": [{"match_id": match_id, **facts(match, collection, names)}], "wickets": wickets, "wicket_fielders": fielders,
            "people": people_rows(match, match_id, names), "innings": innings_rows(match, match_id), "powerplays": powerplay_rows(match, match_id),
            "reviews": review_rows(match, match_id, names), "replacements": replacement_rows(match, match_id, names),
            "missing": missing_rows(match, match_id), "miscounted_overs": miscounted_rows(match, match_id, names),
            "absent_hurt": absent_hurt_rows(match, match_id, names), "dates": date_rows(match, match_id),
            "notes": unsupported_notes(match, match_id, collection)}
