"""The worked examples in the documentation, as data.

Each example is a short chain of real requests. A step names a request, says which values to keep for the next step, and lists the values of the response that the page shows.
The page prints exactly those values, and the tests run every step against the API and fail when the API says anything else, so an example cannot go stale without a test failing.
Every example uses matches that are over (the 2019 Headingley Test and the 2019 World Cup final), so what it shows does not change when new matches are added.

A value in `shows` is a plain value that must match, or a Kind, which only checks that the value is there and of the right sort (a signed cursor, say).
"""
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Kind:
    """A value that varies from request to request. The page shows the description, and the test checks the type."""
    description: str
    python_type: type = str
    length: int | None = None       # the exact length of a list, when it is known


@dataclass(frozen=True)
class Step:
    title: str
    path: str                                   # may hold {name} placeholders, filled from the values kept by earlier steps
    params: dict
    explain: str
    status: int = 200
    take: dict = field(default_factory=dict)    # name -> path in this response of a value to keep for the next steps
    shows: dict = field(default_factory=dict)   # path in this response -> the value shown, and checked
    full_data: bool = False                     # the step needs the full dataset, which has matches the small test set leaves out


@dataclass(frozen=True)
class Example:
    id: str
    title: str
    intro: str
    steps: tuple


PATH = re.compile(r"\.?([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\[([A-Za-z_]+)=([^\]]*)\]")


def lookup(value, path: str):
    """The value at a path such as data[0].match_id or data[name=BA Stokes].player_id. [n] is a position and [key=text] picks the list item whose key equals the text."""
    position = 0
    while position < len(path):
        match = PATH.match(path, position)
        if not match:
            raise KeyError(path)
        name, index, key, wanted = match.groups()
        if name is not None:
            value = value[name]
        elif index is not None:
            value = value[int(index)]
        else:
            value = next(item for item in value if str(item[key]) == wanted)
        position = match.end()
    return value


def fill(text, kept: dict):
    return text.format(**kept) if isinstance(text, str) else text


def fill_params(params: dict, kept: dict) -> dict:
    return {key: fill(value, kept) for key, value in params.items()}


DAY = {"date_from": "2019-08-22", "date_to": "2019-08-22"}
FINAL_DAY = {"date_from": "2019-07-14", "date_to": "2019-07-14"}

EXAMPLES = (
    Example(
        "player", "Find a player and read their batting",
        "Find a player by name, then use the id. The same filters work on every statistics route. Here they pick one day, the third Test of the 2019 Ashes.",
        (
            Step("Search for the player", "/v1/players", {"q": "Stokes"},
                 "A surname finds every player who has it. Choose by name and keep the `player_id`. Names change, ids do not.",
                 take={"player_id": "data[name=BA Stokes].player_id"}, shows={"data[name=BA Stokes].player_id": "e087956b"}),
            Step("Read his batting for that Test", "/v1/players/{player_id}/batting", {"format": "TEST", **DAY},
                 "Filters apply first, then the totals are made from what is left. The average is 143 runs over one dismissal. A rate with nothing to divide by is `null`.",
                 shows={"data.matches": 1, "data.innings": 2, "data.not_outs": 1, "data.runs": 143, "data.balls_faced": 232, "data.highest_score": 135,
                        "data.highest_score_not_out": True, "data.average": 143.0, "data.strike_rate": 61.64}),
            Step("Split it by opponent", "/v1/players/{player_id}/batting/splits", {"by": "opponent", "format": "TEST", **DAY},
                 "The same totals grouped by the side he faced. There is one group because there was one opponent. `key` is the stable id and `label` is the readable name.",
                 shows={"data.by": "opponent", "data.groups[0].key": "Australia", "data.groups[0].label": "Australia", "data.groups[0].runs": 143, "data.groups[0].matches": 1,
                        "next_cursor": None}),
        )),
    Example(
        "match", "Find a match and read its scorecard",
        "Search the matches by words, take the id, and read the match and its scorecard.",
        (
            Step("Search for the match", "/v1/matches", {"q": "England Australia Headingley 2019"},
                 "Four words, each part of a name: both teams, the ground and the season. One match comes back. Keep its `match_id`.",
                 take={"match_id": "data[0].match_id"},
                 shows={"data[0].match_id": 1152848, "data[0].team_1": "England", "data[0].team_2": "Australia", "data[0].venue": "Headingley", "data[0].season": "2019",
                        "data[0].start_date": "2019-08-22", "next_cursor": None}),
            Step("Read the match", "/v1/matches/{match_id}", {},
                 "The match with its innings. England won by one wicket. `actual_overs` is the balls bowled and `scorecard_overs` is the umpires' count.",
                 shows={"data.winner": "England", "data.winner_wickets": 1, "data.format": "TEST", "data.innings[3].batting_team": "England", "data.innings[3].runs": 362,
                        "data.innings[3].wickets": 9, "data.innings[3].actual_overs": "125.4"}),
            Step("Read the scorecard", "/v1/matches/{match_id}/scorecard", {},
                 "England's fourth innings, with the batter who won the match. His `dismissal` is `null` because he was not out.",
                 shows={"data.innings[3].total.runs": 362, "data.innings[3].batting[player=BA Stokes].runs": 135, "data.innings[3].batting[player=BA Stokes].balls": 219,
                        "data.innings[3].batting[player=BA Stokes].dismissal": None}),
        )),
    Example(
        "team", "Find a team and read its record",
        "A team is its name and the gender of the cricket. Records cover this dataset only.",
        (
            Step("Find the team", "/v1/teams", {"q": "England", "gender": "male"},
                 "Use `team` and `gender` exactly as listed.",
                 shows={"data[team=England].gender": "male", "data[team=England].team": "England"}),
            Step("Read its record on the day of the 2019 World Cup final", "/v1/teams/record", {"team": "England", "gender": "male", "format": "ODI", **FINAL_DAY},
                 "The final was tied and went to a Super Over. It is a tie for both teams, with England as the eliminator winner. It is not a win, so `wins` is 0 and `decisive_win_percentage` is `null`.",
                 shows={"data.record.matches": 1, "data.record.wins": 0, "data.record.ties": 1, "data.record.ties_won_eliminator": 1, "data.record.decisive_win_percentage": None,
                        "data.coverage.scope": "this dataset", "data.coverage.matches": 1, "data.coverage.first_match": "2019-07-14"}),
            Step("Read a head-to-head", "/v1/head-to-head", {"team": "England", "opponent": "Australia", "gender": "male", "format": "TEST", "date_from": "2019-08-01", "date_to": "2019-09-30"},
                 "England against Australia in the five Tests of the 2019 Ashes: two wins each and a draw. Swap the teams and wins and losses swap. This one needs the full dataset.",
                 shows={"data.record.matches": 5, "data.record.wins": 2, "data.record.losses": 2, "data.record.draws": 1, "data.record.decisive_win_percentage": 50.0},
                 full_data=True),
        )),
    Example(
        "paging", "Read a long list a page at a time",
        "Pass `next_cursor` back as `cursor` to read on, and stop when it is `null`.",
        (
            Step("The first page", "/v1/matches", {"q": "England", "limit": 2},
                 "Two matches, newest first, and a `next_cursor` because there are more.",
                 take={"cursor": "next_cursor"}, shows={"next_cursor": Kind("a signed cursor")}),
            Step("The next page", "/v1/matches", {"q": "England", "limit": 2, "cursor": "{cursor}"},
                 "The cursor continues where the list stopped. Keep the same `q` and other parameters.",
                 shows={"data": Kind("the next two matches", list, 2)}),
        )),
    Example(
        "errors", "What a refused request looks like",
        "Every error is a problem document with a status, a plain reason and a request id to quote.",
        (
            Step("A missing required parameter", "/v1/teams/record", {"team": "England"},
                 "`gender` is always required for a team.", status=422,
                 shows={"status": 422, "title": "Invalid parameter", "parameter": "gender"}),
            Step("A match that does not exist", "/v1/matches/999999999", {},
                 "An unknown id is a 404. Any unknown route gives the same answer.", status=404, shows={"status": 404, "title": "Not found"}),
        )),
)
