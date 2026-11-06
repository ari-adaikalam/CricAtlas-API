"""The shape of every response, as Pydantic models.

The routes check what they send against these models, and the OpenAPI document is built from them, so the documented
contract and the real JSON cannot drift apart. A model rejects any field it does not list. Field order is the order the
JSON is written in. Match and ball fields come from contract.py, so a column added there reaches the models with it.
"""
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model

from cricket_api import contract, names

STRICT = ConfigDict(extra="forbid", strict=True)


class Model(BaseModel):
    model_config = STRICT


PYTHON_TYPE = {"INTEGER": int, "SMALLINT": int, "DOUBLE": float, "VARCHAR": str, "DATE": date, "BOOLEAN": bool}


class Source(Model):
    name: str
    url: str
    licence: str
    licence_url: str


class Problem(Model):
    """An error, as RFC 9457 problem details."""
    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str | None
    parameter: str | None = None


# A match row: every column of matches.csv. A value can be empty, so every field may be null, but none can be missing.
Match = create_model("Match", __config__=STRICT, **{names.public(c): (PYTHON_TYPE[t] | None, ...) for c, t in contract.MATCHES.items()})

# A ball row. The caller picks the fields, so every field is optional and only the chosen ones are written.
Ball = create_model("Ball", __config__=STRICT, **{names.public(c): (PYTHON_TYPE[t] | None, None) for c, t in contract.BALLS.items()})


class Competition(Model):
    collection: str | None
    event: str | None
    type: str | None
    format: str | None
    gender: str | None
    matches: int
    first_match: date | None
    last_match: date | None


class TeamEntry(Model):
    """A team is its name and the gender of the cricket it played, because the same name is used in both. Matches are the matches it played."""
    team: str
    gender: str | None
    matches: int


class VenueEntry(Model):
    venue: str
    matches: int


class PlayerHit(Model):
    player_id: str | None
    name: str | None


class InningsSummary(Model):
    innings: int
    batting_team: str | None
    target_runs: int | None
    target_overs: float | None
    is_super_over: bool | None
    declared: bool | None
    penalty_runs_pre: int | None
    penalty_runs_post: int | None
    runs: int
    wickets: int
    legal_balls: int
    deliveries: int
    actual_overs: str
    scorecard_overs: str | None


class SquadMember(Model):
    player_id: str | None
    name: str | None


class Official(Model):
    role: str
    name: str | None
    person_id: str | None


MatchDetail = create_model(
    "MatchDetail", __base__=Match, innings=(list[InningsSummary], ...), squads=(dict[str, list[SquadMember]], ...), officials=(list[Official], ...))


class Dismissal(Model):
    kind: str | None
    bowler: str | None
    bowler_id: str | None
    fielders: list[str]
    text: str


class BattingLine(Model):
    order: int
    player: str | None
    player_id: str | None
    runs: int
    balls: int
    fours: int
    sixes: int
    strike_rate: float | None
    dismissal: Dismissal | None


class DidNotBat(Model):
    player: str | None
    player_id: str | None


class FallOfWicket(Model):
    wicket: int
    score: int
    player: str | None
    ball: float | None


class BowlingFigures(Model):
    order: int
    bowler: str | None
    player_id: str | None
    actual_overs: str
    scorecard_overs: str | None
    maidens: int
    runs: int
    wickets: int
    wides: int
    noballs: int
    economy: float | None


class InningsTotal(Model):
    runs: int
    wickets: int
    actual_overs: str
    scorecard_overs: str | None
    legal_balls: int
    run_rate: float | None


class Extras(Model):
    byes: int
    legbyes: int
    wides: int
    noballs: int
    penalty: int
    total: int


class ScorecardInnings(Model):
    innings: int
    batting_team: str | None
    bowling_team: str | None
    is_super_over: bool | None
    declared: bool | None
    total: InningsTotal
    extras: Extras
    batting: list[BattingLine]
    did_not_bat: list[DidNotBat]
    absent_hurt: list[DidNotBat]
    fall_of_wickets: list[FallOfWicket]
    bowling: list[BowlingFigures]


class Scorecard(Model):
    match_id: int
    balls_per_over: int
    innings: list[ScorecardInnings]


class TableInfo(Model):
    rows: int
    columns: list[str]


class Features(Model):
    player_profiles: bool


class DatasetCoverage(Model):
    dataset_label: str | None
    dataset_display_name: str
    first_match_date: str | None
    latest_match_date: str | None
    matches: int
    balls: int
    players: int
    formats: list[str]
    collections: list[str]
    genders: list[str]
    seasons: list[str]


class RefreshStatus(Model):
    record_available: bool
    automatic_verified: bool
    scheduler: Literal["active", "disabled", "unknown", "unrecorded"]
    mode: Literal["manual", "preparation-only", "automatic"]
    status: Literal["unavailable", "idle", "running", "prepared", "unchanged", "failed", "activated", "cache_pending", "rolled_back", "needs_recovery"]
    last_attempt_utc: str | None
    last_successful_check_utc: str | None
    last_successful_refresh_utc: str | None


class Freshness(Model):
    data_version: str
    data_built_utc: str
    snapshot_date: str | None
    coverage: DatasetCoverage
    refresh: RefreshStatus


class MetaData(Model):
    api_version: str
    data_version: str
    data_built_utc: str
    snapshot_date: str | None
    coverage: DatasetCoverage
    features: Features
    tables: dict[str, TableInfo]
    source: str
    source_url: str
    licence: str
    licence_url: str
    attribution: str


class Health(Model):
    status: Literal["ok"]


class Status(Model):
    data_version: str
    data_built_utc: str
    snapshot_date: str | None
    started_utc: str
    requests: int
    client_errors: int
    server_errors: int
    rate_limited: int


# The envelopes. Each data route answers with its data and the source credit. A paged route adds next_cursor, null at the end.
class MetaResponse(Model):
    data: MetaData
    source: Source


class CompetitionsResponse(Model):
    data: list[Competition]
    source: Source


class TeamsResponse(Model):
    data: list[TeamEntry]
    source: Source


class VenuesResponse(Model):
    data: list[VenueEntry]
    source: Source


class PlayersResponse(Model):
    data: list[PlayerHit]
    source: Source


class MatchesResponse(Model):
    data: list[Match]
    next_cursor: str | None
    source: Source


class MatchResponse(Model):
    data: MatchDetail
    source: Source


class ScorecardResponse(Model):
    data: Scorecard
    source: Source


class BallsResponse(Model):
    data: list[Ball]
    next_cursor: str | None
    source: Source


class BattingCareer(Model):
    """A player's batting over the matches the filters select. An innings counts when the player batted: faced a ball, stood at the non-striker's end or was dismissed."""
    player_id: str
    name: str | None
    matches: int
    innings: int
    not_outs: int
    runs: int
    balls_faced: int
    highest_score: int | None
    highest_score_not_out: bool | None
    average: float | None
    strike_rate: float | None
    fifties: int
    hundreds: int
    ducks: int
    fours: int
    sixes: int


class BowlingCareer(Model):
    """A player's bowling over the matches the filters select. Balls are legal deliveries, so a wide or a no ball is not one."""
    player_id: str
    name: str | None
    matches: int
    innings: int
    balls: int
    runs: int
    wickets: int
    maidens: int
    average: float | None
    economy: float | None
    strike_rate: float | None
    best_wickets: int | None
    best_runs: int | None
    four_wicket_hauls: int
    five_wicket_hauls: int
    wides: int
    noballs: int


class BattingResponse(Model):
    data: BattingCareer
    source: Source


class BowlingResponse(Model):
    data: BowlingCareer
    source: Source


class Boundary(Model):
    """One end of a powerplay. over and ball_in_over say where it is, delivery_number is the delivery to join on, listed is the text Cricsheet wrote."""
    over: int | None
    ball_in_over: int | None
    delivery_number: int | None
    listed: str | None


class Powerplay(Model):
    innings: int
    powerplay_number: int
    type: str | None
    start: Boundary
    end: Boundary


class Review(Model):
    review_number: int
    innings: int
    delivery_number: int
    over: int | None
    ball_in_over: int | None
    review_by: str | None
    umpire: str | None
    umpire_id: str | None
    batter: str | None
    batter_id: str | None
    decision: str | None
    review_type: str | None
    umpires_call: bool | None


class Replacement(Model):
    innings: int
    delivery_number: int
    replacement_number: int
    over: int | None
    ball_in_over: int | None
    kind: str | None
    team: str | None
    role: str | None
    player_in: str | None
    player_in_id: str | None
    player_out: str | None
    player_out_id: str | None
    reason: str | None


class WicketFielder(Model):
    fielder_number: int
    fielder: str | None
    fielder_id: str | None
    substitute: bool | None


class MatchWicket(Model):
    """One wicket. sequence counts the wickets of the innings in the order they fell, retirements included."""
    sequence: int
    innings: int
    delivery_number: int
    wicket_number: int
    over: int | None
    ball_in_over: int | None
    player_dismissed: str | None
    player_dismissed_id: str | None
    wicket_type: str | None
    bowler: str | None
    bowler_id: str | None
    credited_to_bowler: bool
    fielders: list[WicketFielder]


class MissingData(Model):
    missing_number: int
    missing_type: str | None
    innings: int | None
    powerplay_type: str | None


class MiscountedOver(Model):
    innings: int
    over: int
    balls: int | None
    umpire: str | None
    umpire_id: str | None


class AbsentHurt(Model):
    innings: int
    player: str | None
    player_id: str | None


class MatchDate(Model):
    date_number: int
    date: date


class Notes(Model):
    missing_data: list[MissingData]
    miscounted_overs: list[MiscountedOver]
    absent_hurt: list[AbsentHurt]
    dates: list[MatchDate]


class PowerplaysResponse(Model):
    data: list[Powerplay]
    source: Source


class ReviewsResponse(Model):
    data: list[Review]
    source: Source


class ReplacementsResponse(Model):
    data: list[Replacement]
    source: Source


class WicketsResponse(Model):
    data: list[MatchWicket]
    source: Source


class NotesResponse(Model):
    data: Notes
    source: Source


class PlayerTeam(Model):
    team: str | None
    matches: int
    first_match: date | None
    last_match: date | None


class ExternalId(Model):
    """An id for the player in another source's system, as Cricsheet's register records it. source is open text: a new source in the register needs no new API version."""
    source: str
    external_id: str


class PlayerProfile(Model):
    """A player as Cricsheet knows them. name is the chosen spelling and aliases are the other spellings the files use, most used first."""
    player_id: str
    name: str | None
    aliases: list[str]
    teams: list[PlayerTeam]
    external_ids: list[ExternalId]


class FieldingCareer(Model):
    """A player's fielding over the matches the filters select. Each count is of distinct wickets, so a player listed twice on a wicket counts once.

    catches is catches_as_fielder plus caught_and_bowled. Substitute involvements are the wickets where the player is recorded as a substitute fielder, and they
    are not in the other counts. The wicket data cannot establish every substitute appearance."""
    player_id: str
    name: str | None
    catches: int
    catches_as_fielder: int
    caught_and_bowled: int
    stumpings: int
    run_out_involvements: int
    substitute_involvements: int


class PlayerResponse(Model):
    data: PlayerProfile
    source: Source


class FieldingResponse(Model):
    data: FieldingCareer
    source: Source


def season_model(name: str, career: type[Model]) -> type[Model]:
    """The career model's fields for one season, with the season and the dates of its first and last match in front. Built from the career model so the two cannot drift."""
    fields = {key: (field.annotation, ...) for key, field in career.model_fields.items() if key not in ("player_id", "name")}
    return create_model(name, __config__=STRICT, season=(str | None, ...), first_match=(date | None, ...), last_match=(date | None, ...), **fields)


BattingSeason = season_model("BattingSeason", BattingCareer)
BowlingSeason = season_model("BowlingSeason", BowlingCareer)
FieldingSeason = season_model("FieldingSeason", FieldingCareer)


class BattingSeasons(Model):
    player_id: str
    name: str | None
    seasons: list[BattingSeason]


class BowlingSeasons(Model):
    player_id: str
    name: str | None
    seasons: list[BowlingSeason]


class FieldingSeasons(Model):
    player_id: str
    name: str | None
    seasons: list[FieldingSeason]


class BattingSeasonsResponse(Model):
    data: BattingSeasons
    source: Source


class BowlingSeasonsResponse(Model):
    data: BowlingSeasons
    source: Source


class FieldingSeasonsResponse(Model):
    data: FieldingSeasons
    source: Source


def totals_model(name: str, career: type[Model]) -> type[Model]:
    """The career model's totals without the player's identity, as the totals under a leaderboard row. Built from the career model so the two cannot drift."""
    return create_model(name, __config__=STRICT, **{key: (field.annotation, ...) for key, field in career.model_fields.items() if key not in ("player_id", "name")})


BattingTotals = totals_model("BattingTotals", BattingCareer)
BowlingTotals = totals_model("BowlingTotals", BowlingCareer)


class BattingEligibility(Model):
    min_balls: int | None
    min_dismissals: int | None


class BowlingEligibility(Model):
    min_balls: int | None
    min_wickets: int | None


class BattingLeader(Model):
    """rank is the position in the list. value is what the list is ranked by (the wickets for best figures, with the runs in totals)."""
    rank: int
    player_id: str
    name: str | None
    value: float | int
    totals: BattingTotals


class BowlingLeader(Model):
    rank: int
    player_id: str
    name: str | None
    value: float | int
    totals: BowlingTotals


class BattingLeaderboard(Model):
    metric: str
    order: Literal["desc", "asc"]
    eligibility: BattingEligibility
    limit: int
    leaders: list[BattingLeader]


class BowlingLeaderboard(Model):
    metric: str
    order: Literal["desc", "asc"]
    eligibility: BowlingEligibility
    limit: int
    leaders: list[BowlingLeader]


class BattingLeaderboardResponse(Model):
    data: BattingLeaderboard
    source: Source


class BowlingLeaderboardResponse(Model):
    data: BowlingLeaderboard
    source: Source


class MatchupPlayer(Model):
    player_id: str
    name: str | None


class DismissalKind(Model):
    kind: str
    count: int


class Matchup(Model):
    """One batter against one bowler. dismissals are every way the batter went out on this bowler's deliveries, a run out at the non-striker's end included, and
    bowler_wickets are the credited kinds only. Retired hurt and retired not out are not dismissals. Rates are from the summed figures and null when there is nothing to divide by."""
    batter: MatchupPlayer
    bowler: MatchupPlayer
    matches: int
    innings: int
    deliveries: int
    balls_faced: int
    runs: int
    fours: int
    sixes: int
    strike_rate: float | None
    dismissals: int
    bowler_wickets: int
    average: float | None
    balls_per_dismissal: float | None
    dismissal_kinds: list[DismissalKind]


class MatchupResponse(Model):
    data: Matchup
    source: Source


class PlayerMatch(Model):
    """A match the data connects to the player, with what each kind of record says. recorded_on_field is true when the player batted, bowled or fielded (as a named
    fielder or a substitute) and is never set by squad membership, an absent hurt entry or a replacement entry. super_over says one of those records is in a Super Over."""
    match_id: int
    start_date: date | None
    season: str | None
    event: str | None
    collection: str | None
    format: str | None
    gender: str | None
    venue: str | None
    city: str | None
    team_1: str | None
    team_2: str | None
    winner: str | None
    outcome: str | None
    team: str | None
    in_squad: bool
    batted: bool
    bowled: bool
    fielded: bool
    fielded_as_substitute: bool
    absent_hurt: bool
    replaced_in: bool
    replaced_out: bool
    super_over: bool
    recorded_on_field: bool


class PlayerMatchesResponse(Model):
    data: list[PlayerMatch]
    next_cursor: str | None
    source: Source


def split_model(name: str, career: type[Model], leading: tuple = ()) -> type[Model]:
    """One group of a split: its key and readable label, then the career model's totals for that group. Built from the career model so the two cannot drift. A group's
    highest score and best figures are its own best, and each rate is made from the group's own sums."""
    fields = {key: (field.annotation, ...) for key, field in career.model_fields.items() if key not in ("player_id", "name")}
    return create_model(name, __config__=STRICT, key=(str | None, ...), label=(str | None, ...), **dict(leading), **fields)


SplitBy = Literal["opponent", "venue", "competition", "format"]
BattingSplit = split_model("BattingSplit", BattingCareer)
BowlingSplit = split_model("BowlingSplit", BowlingCareer)
FieldingSplit = split_model("FieldingSplit", FieldingCareer, leading=(("matches", (int, ...)),))


class BattingSplits(Model):
    player_id: str
    name: str | None
    by: SplitBy
    groups: list[BattingSplit]


class BowlingSplits(Model):
    player_id: str
    name: str | None
    by: SplitBy
    groups: list[BowlingSplit]


class FieldingSplits(Model):
    player_id: str
    name: str | None
    by: SplitBy
    groups: list[FieldingSplit]


class BattingSplitsResponse(Model):
    data: BattingSplits
    next_cursor: str | None
    source: Source


class BowlingSplitsResponse(Model):
    data: BowlingSplits
    next_cursor: str | None
    source: Source


class FieldingSplitsResponse(Model):
    data: FieldingSplits
    next_cursor: str | None
    source: Source


class Coverage(Model):
    """What a record covers. It is always this dataset, never a complete history. matches and the dates
    describe the whole filtered result, not the page that was returned, and the dates are null when the result is empty."""
    scope: Literal["this dataset"]
    matches: int
    first_match: date | None
    last_match: date | None
    note: str


class TeamRecord(Model):
    """The results of one team over a set of matches, each match counted once. matches is wins + losses + draws + ties + no_results, and ties is ties_won_eliminator +
    ties_lost_eliminator + ties_no_eliminator. A recorded tie is never a win, and the eliminator that settled it is shown beside it. no_results includes matches abandoned
    after play began. decisive_win_percentage is wins over wins plus losses, to two places, and null when there is none."""
    matches: int
    wins: int
    losses: int
    draws: int
    ties: int
    no_results: int
    ties_won_eliminator: int
    ties_lost_eliminator: int
    ties_no_eliminator: int
    decisive_win_percentage: float | None


class TeamRecordData(Model):
    team: str
    gender: Literal["male", "female"]
    record: TeamRecord
    coverage: Coverage


class HeadToHeadData(Model):
    """The record of team against opponent, from the point of view of team. Swapping the two swaps wins with losses and ties_won_eliminator with ties_lost_eliminator."""
    team: str
    opponent: str
    gender: Literal["male", "female"]
    record: TeamRecord
    coverage: Coverage


RecordSplit = create_model("RecordSplit", __config__=STRICT, key=(str | None, ...), label=(str | None, ...), **{key: (field.annotation, ...) for key, field in TeamRecord.model_fields.items()})
RecordBy = Literal["opponent", "venue", "competition", "format", "season"]
HeadToHeadBy = Literal["venue", "competition", "format", "season"]


class TeamSplitsData(Model):
    team: str
    gender: Literal["male", "female"]
    by: RecordBy
    coverage: Coverage
    groups: list[RecordSplit]


class HeadToHeadSplitsData(Model):
    team: str
    opponent: str
    gender: Literal["male", "female"]
    by: HeadToHeadBy
    coverage: Coverage
    groups: list[RecordSplit]


class TeamRecordResponse(Model):
    data: TeamRecordData
    source: Source


class HeadToHeadResponse(Model):
    data: HeadToHeadData
    source: Source


class TeamSplitsResponse(Model):
    data: TeamSplitsData
    next_cursor: str | None
    source: Source


class HeadToHeadSplitsResponse(Model):
    data: HeadToHeadSplitsData
    next_cursor: str | None
    source: Source
