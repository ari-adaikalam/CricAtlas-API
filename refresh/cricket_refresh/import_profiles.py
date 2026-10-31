"""Import updated player profile data from the repository's dataset pipeline into the format the API needs.

    python -m cricket_refresh.import_profiles [--from data] [--apply]

This is the only code in api/ that reads the repository's data/ folder (players/player_ids.csv and players/raw/profiles.jsonl). A normal refresh
never does: it reads the latest successfully imported snapshot, named by api/data/inputs/profiles/latest.json.

Without --apply it reads, validates and reports, and writes nothing. With --apply it:
  1. builds the table (Cricsheet player id, name, country, batting style, bowling style, playing role, birth date; the exact birth date stays in this private
     gitignored input and is only used to compute ages, it never reaches an export);
  2. validates it (below) and refuses to import if a check fails;
  3. writes the snapshot into a temporary folder next to its final place, then moves the folder into place and only then replaces latest.json, each in one atomic step,
     so a crash leaves the previous snapshot as the latest;
  4. keeps every earlier snapshot.

Checks: every identifier is a Cricsheet id (8 hex characters), none is empty or repeated; no identifier of the latest snapshot has gone (ids are stable, a player
is never dropped, a profile that is missing in the new data is kept as it was in the old snapshot only if --keep-missing is given, otherwise the import stops and lists them);
names that changed for an existing id are counted and listed; the row count does not fall.
A player without a profile stays a row with empty profile columns. Missing profiles never remove a player or a statistic.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from cricket_refresh.config import profile_snapshot as config_profile_snapshot, profiles_dir
from cricket_refresh.locking import AlreadyRunning, RunLock
from cricket_refresh.profiles import SNAPSHOT_COLUMNS

FIELDS = {"country": "country", "batting_style": "batting style", "bowling_style": "bowling style", "playing_roles": "playing role"}
SOURCE_FILES = {"player_ids.csv": "players/player_ids.csv", "profiles.jsonl": "players/raw/profiles.jsonl"}
ID_PATTERN = r"[0-9a-f]{8}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def _joined(value):
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or None
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value or None


def _birth_date(value):
    if not isinstance(value, dict):
        return pd.NaT
    try:
        return pd.Timestamp(year=int(value["year"]), month=int(value["month"]), day=int(value["date"]))
    except (KeyError, TypeError, ValueError):
        return pd.NaT


def read_profiles(path: Path) -> pd.DataFrame:
    """One row per Cricinfo player id, a later line replacing an earlier one, only the good (status 200) profiles. The labels are used as they come."""
    latest = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue                   # a half written line from a stop
            latest[int(row["cricinfo_id"])] = row
    good = [row for row in latest.values() if row.get("status") == 200 and not row.get("error")]
    table = pd.DataFrame(good, columns=None) if good else pd.DataFrame(columns=["cricinfo_id", "dob", *FIELDS])
    for field in FIELDS:
        if field not in table.columns:
            table[field] = None
        table[field] = table[field].map(_joined)
    table["birth_date"] = table["dob"].map(_birth_date) if "dob" in table.columns else pd.NaT
    return table[["cricinfo_id", *FIELDS, "birth_date"]].rename(columns=FIELDS)


def make_table(source: Path) -> pd.DataFrame:
    ids = pd.read_csv(source / "players" / "player_ids.csv", dtype=str)[["identifier", "name", "cricinfo_id"]]
    ids["cricinfo_id"] = pd.to_numeric(ids["cricinfo_id"], errors="coerce")
    profiles = read_profiles(source / "players" / "raw" / "profiles.jsonl")
    table = ids.merge(profiles, on="cricinfo_id", how="left")                      # left: a player with no profile stays, with empty columns
    table["birth_date"] = pd.to_datetime(table["birth_date"]).dt.strftime("%Y-%m-%d")
    return table[SNAPSHOT_COLUMNS]


def latest_snapshot() -> tuple[str, pd.DataFrame] | None:
    pointer = profiles_dir() / "latest.json"
    if not pointer.exists():
        return None
    path = config_profile_snapshot()
    name = path.parent.name
    return name, pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


def validate(table: pd.DataFrame, previous: pd.DataFrame | None, keep_missing: bool) -> tuple[list[str], dict]:
    """Returns the problems that stop the import and facts to report."""
    problems, facts = [], {}
    ids = table["identifier"]
    if ids.isna().any() or (ids.astype(str).str.strip() == "").any():
        problems.append("an identifier is empty")
    bad = ids[~ids.astype(str).str.fullmatch(ID_PATTERN)]
    if len(bad):
        problems.append(f"{len(bad)} identifiers are not Cricsheet ids (8 hex characters), for example {list(bad[:3])}")
    if ids.duplicated().any():
        problems.append(f"{int(ids.duplicated().sum())} identifiers appear more than once")
    if table["name"].isna().any():
        problems.append(f"{int(table['name'].isna().sum())} players have no name")
    if previous is not None:
        gone = sorted(set(previous["identifier"]) - set(ids))
        facts["identifiers_gone"] = len(gone)
        if gone and not keep_missing:
            problems.append(f"{len(gone)} identifiers of the latest snapshot are not in the new data, for example {gone[:3]} (ids must stay stable; use --keep-missing to carry them over)")
        both = previous.merge(table, on="identifier", suffixes=("_old", "_new"))
        facts["names_changed"] = int((both["name_old"].fillna("") != both["name_new"].fillna("")).sum())
        facts["new_identifiers"] = len(set(ids) - set(previous["identifier"]))
        if len(table) < len(previous) and not keep_missing:
            problems.append(f"the row count fell from {len(previous)} to {len(table)}")
    return problems, facts


def carry_over(table: pd.DataFrame, previous: pd.DataFrame) -> pd.DataFrame:
    kept = previous[~previous["identifier"].isin(table["identifier"])]
    return pd.concat([table, kept[SNAPSHOT_COLUMNS]], ignore_index=True)


def write_atomically(table: pd.DataFrame, provenance: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = table.to_csv(index=False).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    name = f"{stamp}-{digest[:8]}"
    final = profiles_dir() / name
    temp = profiles_dir() / f".{name}.tmp"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir()
    os.chmod(temp, 0o700)
    with open(os.open(temp / "player_profiles.csv", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    provenance = {**provenance, "snapshot": name, "snapshot_sha256": digest, "rows": len(table), "columns": SNAPSHOT_COLUMNS}
    (temp / "provenance.json").write_text(json.dumps(provenance, indent=1), encoding="utf-8")
    os.rename(temp, final)                                              # one step: the folder is complete or absent
    pointer_temp = profiles_dir() / ".latest.json.tmp"
    pointer_temp.write_text(json.dumps({"snapshot": name, "sha256": digest, "imported_utc": provenance["imported_utc"]}, indent=1), encoding="utf-8")
    with open(pointer_temp, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(pointer_temp, profiles_dir() / "latest.json")            # the previous snapshot stays the latest until this succeeds
    return name


def run(source: Path, apply: bool, keep_missing: bool) -> int:
    try:
        with RunLock(profiles_dir() / ".import.lock", "profile import"):
            return _run(source, apply, keep_missing)
    except AlreadyRunning as error:
        print(error)
        return 1


def _run(source: Path, apply: bool, keep_missing: bool) -> int:
    missing = [str(source / rel) for rel in SOURCE_FILES.values() if not (source / rel).exists()]
    if missing:
        print("missing:", *missing, sep="\n  ")
        return 1
    table = make_table(source)
    latest = latest_snapshot()
    previous = latest[1] if latest else None
    problems, facts = validate(table, previous, keep_missing)
    if keep_missing and previous is not None and not problems:
        table = carry_over(table, previous)
    print(f"{len(table):,} players, {table['country'].notna().sum():,} with a country, {table['birth_date'].notna().sum():,} with a birth date")
    print("previous snapshot:", latest[0] if latest else "none", "|", facts or "no comparison")
    if problems:
        print("NOT IMPORTED:", *problems, sep="\n  ")
        return 1
    if not apply:
        print("dry run: nothing written (use --apply)")
        return 0
    provenance = {
        "imported_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "source_folder": "the repository's data/ (players/player_ids.csv, players/raw/profiles.jsonl)",
        "sources": {name: {"bytes": (source / rel).stat().st_size, "sha256": sha256(source / rel)} for name, rel in SOURCE_FILES.items()},
        "previous_snapshot": latest[0] if latest else None, "comparison": facts, "kept_missing": keep_missing,
        "method": "Cricsheet player id joined to the player crosswalk, then to the saved profile of the matching Cricinfo player (labels used as they come, no mapping). "
                  "A player without a profile stays, with empty profile columns.",
        "credit": "Profile labels come from Cricinfo's player pages.",
    }
    if latest and hashlib.sha256(table.to_csv(index=False).encode("utf-8")).hexdigest() == json.loads((profiles_dir() / "latest.json").read_text())["sha256"]:
        print(f"the data is identical to the latest snapshot ({latest[0]}); nothing written")
        return 0
    name = write_atomically(table, provenance)
    print(f"imported as {name}; it is now the latest snapshot. Earlier snapshots are kept in {profiles_dir()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cricket_refresh.import_profiles")
    parser.add_argument("--from", dest="source", default="data", help="the repository's data folder (default: data)")
    parser.add_argument("--apply", action="store_true", help="write the snapshot (without it, only read, validate and report)")
    parser.add_argument("--keep-missing", action="store_true", help="carry over players of the latest snapshot that the new data lacks")
    args = parser.parse_args(argv)
    return run(Path(args.source), args.apply, args.keep_missing)


if __name__ == "__main__":
    sys.exit(main())
