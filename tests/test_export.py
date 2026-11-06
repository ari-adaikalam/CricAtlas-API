import json

import duckdb
import pytest

from cricket_api import contract, export

BALL_HEADER = list(contract.BALLS) + ["ball_line", "wagon_x"]
MATCH_HEADER = list(contract.MATCHES)
PLAYER_HEADER = ["player_id", "name", "cricinfo_id", "full name", "country"]


def ball_row(match=1, innings=1, delivery=1, **extra):
    row = {c: "" for c in BALL_HEADER}
    row.update({"match_id": match, "season": "2001/02", "start_date": "2001-12-19", "innings": innings, "delivery_number": delivery,
                "over": 0, "ball_in_over": delivery, "scorecard_ball": "0.1", "is_legal_delivery": "True", "striker": "A Bat",
                "striker_id": "p1", "bowler": "B Bowl", "bowler_id": "p2", "runs_off_bat": 4, "extras": 0, "wicket": 0,
                "striker balls faced": delivery, "total striker runs": 4, "gender": "male", "event": "Test series", "format": "TEST",
                "type": "Intl", "batting_team": "X", "bowling_team": "Y", "ball_line": "on the stumps", "country_striker": "X",
                "striker_age": 29, "wagon_x": 200})
    row.update(extra)
    return row


def write_csv(path, header, rows):
    lines = [",".join(f'"{h}"' if " " in h else h for h in header)]
    for row in rows:
        lines.append(",".join(str(row.get(h, "")) for h in header))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "output"
    folder.mkdir()
    write_csv(folder / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 2), ball_row(1, 1, 1), ball_row(2, 1, 1, wides=1, is_legal_delivery="False")])
    write_csv(folder / "matches.csv", MATCH_HEADER, [{"match_id": 1, "season": "2001/02", "start_date": "2001-12-19", "gender": "male", "venue": "A Ground", "winner": "X"},
                                                     {"match_id": 2, "season": "2001/02", "start_date": "2001-12-20", "gender": "male", "winner": "Y", "winner_runs": 7}])
    for name in contract.csv_tables():
        if name not in ("balls", "matches", "players"):
            write_csv(folder / contract.TABLES[name]["source"], list(contract.TABLES[name]["columns"]), [])
    write_csv(folder / "players.csv", PLAYER_HEADER, [{"player_id": "p1", "name": "A Bat", "cricinfo_id": 5, "full name": "Alan Bat", "country": "X"},
                                                      {"player_id": "p2", "name": "B Bowl", "cricinfo_id": 6, "full name": "Bob Bowl", "country": "Y"}])
    return folder


def read(path, sql):
    return duckdb.connect().execute(sql.format(p=f"read_parquet('{path}')")).fetchall()


def test_the_export_writes_the_three_tables_and_meta(source, tmp_path):
    out = tmp_path / "api"
    meta, problems = export.build(source, out)
    assert problems == [] and meta["tables"]["balls"]["rows"] == 3
    assert sorted(p.name for p in out.iterdir()) == sorted([f"{t}.parquet" for t in contract.TABLES] + ["meta.json"])
    assert json.loads((out / "meta.json").read_text())["data_version"] == meta["data_version"]


def test_only_allowed_columns_reach_the_files(source, tmp_path):
    out = tmp_path / "api"
    export.build(source, out)
    balls = [r[0] for r in read(out / "balls.parquet", "DESCRIBE SELECT * FROM {p}")]
    players = [r[0] for r in read(out / "players.parquet", "DESCRIBE SELECT * FROM {p}")]
    assert balls == list(contract.BALLS) and players == ["player_id", "name"]
    assert "ball_line" not in balls and "wagon_x" not in balls and "country_striker" in balls
    for column in balls + players:
        assert not contract.banned(column)


def test_types_are_set_on_purpose(source, tmp_path):
    out = tmp_path / "api"
    export.build(source, out)
    types = dict(read(out / "balls.parquet", "SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM {p})"))
    assert types["season"] == "VARCHAR" and types["start_date"] == "DATE" and types["is_legal_delivery"] == "BOOLEAN"
    assert types["wides"] == "SMALLINT" and types["scorecard_ball"] == "DOUBLE"
    assert read(out / "balls.parquet", "SELECT season FROM {p} LIMIT 1")[0][0] == "2001/02"


def test_rows_are_sorted_so_a_match_sits_together(source, tmp_path):
    out = tmp_path / "api"
    export.build(source, out)
    keys = read(out / "balls.parquet", "SELECT match_id, innings, delivery_number FROM {p}")
    assert keys == [(1, 1, 1), (1, 1, 2), (2, 1, 1)]


def test_blank_cells_stay_empty_and_values_survive(source, tmp_path):
    out = tmp_path / "api"
    export.build(source, out)
    rows = read(out / "balls.parquet", "SELECT wides, is_legal_delivery, runs_off_bat FROM {p} WHERE match_id = 2")
    assert rows == [(1, False, 4)]
    assert read(out / "balls.parquet", "SELECT wides FROM {p} WHERE match_id = 1")[0][0] is None


def test_a_repeated_key_stops_the_export_and_leaves_the_old_files(source, tmp_path):
    out = tmp_path / "api"
    export.build(source, out)
    before = (out / "meta.json").read_text()
    write_csv(source / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 1), ball_row(1, 1, 1)])
    meta, problems = export.build(source, out)
    assert meta is None and any("repeat the key" in p for p in problems)
    assert (out / "meta.json").read_text() == before
    assert not list(tmp_path.glob(".*building"))


def test_a_ball_for_a_player_who_is_not_in_players_csv_still_has_a_player_row(source, tmp_path):
    """The player table is built from every file that names a player, so a striker players.csv misses is still a player, under the name on the ball row."""
    write_csv(source / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 1, striker_id="unknown", striker="Z Unknown"), ball_row(2, 1, 1)])
    out = tmp_path / "api"
    meta, problems = export.build(source, out)
    assert problems == [], problems
    assert ("unknown", "Z Unknown") in read(out / "players.parquet", "SELECT player_id, name FROM {p}")
    assert meta["player_universe"]["added"] == 1


def test_people_known_only_as_officials_are_not_players(source, tmp_path):
    write_csv(source / "match_people.csv", ["match_id", "role", "person_number", "team", "person", "person_id"],
              [{"match_id": 1, "role": "umpire", "person_number": 1, "person": "U Umpire", "person_id": "u9"},
               {"match_id": 1, "role": "player", "person_number": 1, "team": "X", "person": "S Squad", "person_id": "s9"}])
    write_csv(source / "player_external_ids.csv", ["player_id", "source", "external_id"], [{"player_id": "x9", "source": "bcci", "external_id": "7"}])
    out = tmp_path / "api"
    meta, problems = export.build(source, out)
    assert problems == [], problems
    ids = {r[0] for r in read(out / "players.parquet", "SELECT player_id FROM {p}")}
    assert "s9" in ids and "u9" not in ids and "x9" not in ids, "a squad player is a player, an umpire is not, and an external id alone proves nothing"


def test_the_chosen_name_is_deterministic_and_every_spelling_is_kept(source, tmp_path):
    names = [{"match_id": 1, "role": "player", "person_number": n, "team": "X", "person": spelling, "person_id": "p1"} for n, spelling in enumerate(["A Bat (2)", "A Bat", "A Bat"], 1)]
    write_csv(source / "match_people.csv", ["match_id", "role", "person_number", "team", "person", "person_id"], names)
    out = tmp_path / "api"
    meta, problems = export.build(source, out)
    assert problems == [], problems
    assert read(out / "players.parquet", "SELECT name FROM {p} WHERE player_id = 'p1'") == [("A Bat",)], "players.csv comes first"
    spellings = dict(read(out / "player_names.parquet", "SELECT name, uses FROM {p} WHERE player_id = 'p1'"))
    assert set(spellings) == {"A Bat", "A Bat (2)"} and spellings["A Bat (2)"] == 1 and spellings["A Bat"] >= 3, spellings
    assert meta["player_universe"]["ids_with_more_than_one_spelling"] >= 1 and meta["player_universe"]["ids_with_conflicting_spellings"] == 0, "(2) is not a conflict"


def test_a_match_without_balls_stops_the_export(source, tmp_path):
    write_csv(source / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 1)])
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and any("no balls" in p for p in problems)


def test_the_meta_says_whether_player_profiles_are_in_the_data(source, tmp_path):
    meta, _ = export.build(source, tmp_path / "api")
    assert meta["features"] == {"player_profiles": True} and "country_striker" in meta["tables"]["balls"]["columns"]


def test_without_the_profile_columns_the_export_follows_the_files(source, tmp_path):
    lines = (source / "ball_by_ball_data.csv").read_text().splitlines()
    header = lines[0].split(",")
    keep = [i for i, c in enumerate(header) if c.strip('"') not in contract.PROFILE_BALL]
    import csv, io
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in csv.reader(lines):
        writer.writerow([row[i] for i in keep])
    (source / "ball_by_ball_data.csv").write_text(out.getvalue())
    meta, problems = export.build(source, tmp_path / "api")
    assert problems == [] and meta["features"] == {"player_profiles": False}
    columns = meta["tables"]["balls"]["columns"]
    assert "country_striker" not in columns and "striker_age" not in columns and "striker" in columns


def test_a_ball_file_with_only_some_profile_columns_is_refused(source, tmp_path):
    text = (source / "ball_by_ball_data.csv").read_text().replace("striker_age", "striker_age_x")
    (source / "ball_by_ball_data.csv").write_text(text)
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and "some of the player profile columns" in problems[0]


def test_a_missing_source_file_is_reported(source, tmp_path):
    (source / "players.csv").unlink()
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and "players.csv is missing" in problems[0]


def test_the_data_version_changes_only_when_the_data_does(source, tmp_path):
    first, _ = export.build(source, tmp_path / "a")
    second, _ = export.build(source, tmp_path / "b")
    assert first["data_version"] == second["data_version"]
    write_csv(source / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 1, runs_off_bat=6), ball_row(2, 1, 1)])
    third, _ = export.build(source, tmp_path / "c")
    assert third["data_version"] != first["data_version"]


def test_the_snapshot_date_comes_from_the_manifest_when_there_is_one(source, tmp_path):
    meta, _ = export.build(source, tmp_path / "a")
    assert meta["snapshot_date"] is None
    (source / "manifest.json").write_text(json.dumps({"snapshot_date": "2026-10-09"}))
    meta, _ = export.build(source, tmp_path / "b")
    assert meta["snapshot_date"] == "2026-10-09"


def test_a_quote_in_a_folder_name_does_not_break_the_queries(source, tmp_path):
    odd = tmp_path / "it's here"
    odd.mkdir()
    for name in [spec["source"] for spec in contract.csv_tables().values()]:
        (odd / name).write_text((source / name).read_text())
    meta, problems = export.build(odd, tmp_path / "o'ut" / "api")
    assert problems == [] and meta["tables"]["balls"]["rows"] == 3


def test_the_meta_carries_the_source_and_the_licence_terms(source, tmp_path):
    meta, _ = export.build(source, tmp_path / "api")
    assert meta["source"] == "Cricsheet" and meta["source_url"] == "https://cricsheet.org/"
    assert "ODC-By" in meta["attribution"] and meta["licence_url"].startswith("https://opendatacommons.org/licenses/by/1-0")


def test_an_external_id_from_a_banned_source_stops_the_export(source, tmp_path):
    write_csv(source / "player_external_ids.csv", ["player_id", "source", "external_id"], [{"player_id": "p1", "source": "cricinfo", "external_id": "5"}])
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and any("banned" in p and "cricinfo" in p for p in problems)


def test_spellings_are_counted_only_in_the_highest_source_that_has_the_player(source, tmp_path):
    """Ball rows say Alpha twice and Beta once. The squad lists say Beta twice. Beta has more rows overall, but the ball rows come first, so the name is Alpha."""
    write_csv(source / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 1, striker_id="px", striker="Alpha"), ball_row(1, 1, 2, striker_id="px", striker="Alpha"),
                                                              ball_row(1, 1, 3, striker_id="px", striker="Beta"), ball_row(2, 1, 1)])
    write_csv(source / "match_people.csv", ["match_id", "role", "person_number", "team", "person", "person_id"],
              [{"match_id": 1, "role": "player", "person_number": n, "team": "X", "person": "Beta", "person_id": "px"} for n in (1, 2)])
    out = tmp_path / "api"
    meta, problems = export.build(source, out)
    assert problems == [], problems
    assert read(out / "players.parquet", "SELECT name FROM {p} WHERE player_id = 'px'") == [("Alpha",)]
    assert dict(read(out / "player_names.parquet", "SELECT name, uses FROM {p} WHERE player_id = 'px'")) == {"Alpha": 2, "Beta": 3}, "every spelling and its total use is still kept"


def test_a_tie_in_the_highest_source_goes_to_the_name_that_sorts_first(source, tmp_path):
    write_csv(source / "ball_by_ball_data.csv", BALL_HEADER, [ball_row(1, 1, 1, striker_id="px", striker="Zed"), ball_row(1, 1, 2, striker_id="px", striker="Abe"), ball_row(2, 1, 1)])
    out = tmp_path / "api"
    export.build(source, out)
    assert read(out / "players.parquet", "SELECT name FROM {p} WHERE player_id = 'px'") == [("Abe",)]


def test_a_match_with_no_winner_and_an_outcome_the_records_cannot_count_stops_the_export(source, tmp_path):
    write_csv(source / "matches.csv", MATCH_HEADER, [{"match_id": 1, "season": "2001/02", "start_date": "2001-12-19", "gender": "male", "venue": "A Ground", "winner": "X"},
                                                     {"match_id": 2, "season": "2001/02", "start_date": "2001-12-20", "gender": "male", "outcome": "abandoned"}])
    meta, problems = export.build(source, tmp_path / "api")
    assert meta is None and any("abandoned" in p and "cannot count" in p for p in problems), problems
    write_csv(source / "matches.csv", MATCH_HEADER, [{"match_id": 1, "season": "2001/02", "start_date": "2001-12-19", "gender": "male", "venue": "A Ground", "winner": "X"},
                                                     {"match_id": 2, "season": "2001/02", "start_date": "2001-12-20", "gender": "male", "outcome": "no result"}])
    assert export.build(source, tmp_path / "api2")[1] == []
