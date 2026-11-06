"""Cut the real matches the golden tests use out of the full dataset.

    python tests/golden/build_golden.py [--source ../data/output]

Run it from the api folder on the machine that holds the full build. It copies every row of the chosen matches from each
CSV, runs the real export over them, and writes the Parquet files and pins.json next to this script. The Parquet files are
committed, so the tests run anywhere without the full data. Re-run it only to add a match or to move to a newer snapshot,
and then re-check the expected values in test_golden.py against the new rows.
"""
import argparse
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
from cricket_api import contract, export  # noqa: E402

# role of each match in the tests
MATCHES = {
    1152848: "Test, England v Australia, Headingley 2019 (Stokes 135 not out, England won by 1 wicket)",
    1528547: "Test, New Zealand v Ireland 2026: Ireland followed on and a declaration, New Zealand won by an innings",
    1144530: "ODI, 2019 World Cup final: tied, with a Super Over",
    1532481: "ODI, Australia v Bangladesh 2026: result by Duckworth-Lewis, chased",
    1521263: "The Hundred, Manchester Super Giants 2026: five-ball sets",
    1483765: "Match with ten retirements recorded as retired out",
    1552797: "Match with a batter retired hurt",
    1030217: "Penalty runs awarded before the innings",
    1138202: "Penalty runs awarded after the innings",
    64012: "Test with an over of seven balls (miscounted overs)",
    1109602: "Test with three innings where a batter was absent hurt",
    668511: "ODI whose second batting powerplay starts at an over that has no single delivery (unresolved start)",
    1523981: "ODI whose mandatory powerplay ends at a position that matches no delivery (unresolved end)",
    1370352: "T20 with four replacements",
    296687: "ODI with five notes of missing data",
    1543889: "T20 where a substitute fielder who is nothing else took a catch",
    995469: "ODI with a run out credited to two fielders",
    1394759: "Match where the same fielder id is listed twice on one wicket",
    64080: "Test, West Indies v England, Antigua 2004: Lara's 400 not out, a draw",
    1278675: "Test, India v New Zealand, Mumbai 2021: Ajaz Patel's 10 wickets in an innings, India won by 372 runs",
    1529284: "IPL T20 where an impact player came in by a replacement entry and has no other record in the match",
    1298152: "T20 whose over 8 of the first innings holds seven legal balls that nobody listed as miscounted",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(HERE.parent.parent.parent / "data" / "output"))
    args = parser.parse_args()
    source = Path(args.source)
    ids = ", ".join(str(i) for i in MATCHES)
    con = duckdb.connect()
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        for name, spec in contract.csv_tables().items():
            read = f"read_csv('{source / spec['source']}', header = true, all_varchar = true)"
            if name == "players":
                where = f"player_id IN (SELECT striker_id FROM read_csv('{scratch / 'ball_by_ball_data.csv'}', header = true, all_varchar = true) " \
                        f"UNION SELECT bowler_id FROM read_csv('{scratch / 'ball_by_ball_data.csv'}', header = true, all_varchar = true))"
            elif name == "player_external_ids":
                cut = lambda file, column: f"SELECT {column} FROM read_csv('{scratch / file}', header = true, all_varchar = true)"
                where = (f"player_id IN ({cut('players.csv', 'player_id')} UNION {cut('wicket_fielders.csv', 'fielder_id')} UNION {cut('innings_absent_hurt.csv', 'player_id')} "
                         f"UNION {cut('match_replacements.csv', 'player_in_id')} UNION {cut('match_replacements.csv', 'player_out_id')} "
                         f"UNION SELECT person_id FROM read_csv('{scratch / 'match_people.csv'}', header = true, all_varchar = true) WHERE role = 'player')")
            else:
                where = f"match_id IN ({ids})"
            con.execute(f"COPY (SELECT * FROM {read} WHERE {where}) TO '{scratch / spec['source']}' (HEADER, DELIMITER ',')")
        for old in HERE.glob("api"):
            shutil.rmtree(old)
        meta, problems = export.build(scratch, HERE / "api")
        assert not problems, problems
    full = json.loads((source.parent / "api" / "meta.json").read_text()) if (source.parent / "api" / "meta.json").exists() else {}
    pins = {"made_on": date.today().isoformat(), "snapshot_date": full.get("snapshot_date"), "full_data_version": full.get("data_version"),
            "fixture_data_version": meta["data_version"], "matches": {str(k): v for k, v in MATCHES.items()}}
    (HERE / "pins.json").write_text(json.dumps(pins, indent=1) + "\n")
    print("wrote", HERE / "api", "and pins.json")


if __name__ == "__main__":
    main()
