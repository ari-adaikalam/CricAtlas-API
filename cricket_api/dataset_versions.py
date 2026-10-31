"""Human-readable names for data versions: "Dataset v1", "Dataset v2", and so on.

A label marks a milestone: a change of schema, contract, coverage or definitions that the owner decides deserves a number. It is not a log of refreshes.
A daily update of the same kind of data takes no label, the daily publisher registers none (a test holds that), and the documentation names such data by the
date of its Cricsheet snapshot. The counter therefore moves only when someone chooses to move it.

The technical identifier of a data set is its `data_version`, a hash of its files. It stays what it is and keeps keying caches, cursors and ETags. This module only gives each hash a friendly name, once, in
`dataset_versions.json`, which is part of the package and so ships inside the API image. The file is append-only: a hash that is already in it keeps its label forever, so rebuilding or redeploying the same data never
changes the name, and a new set of data (a new hash) takes the next number.

    python -m cricket_api.dataset_versions show                  # the registry
    python -m cricket_api.dataset_versions register DATA_DIR     # give the data in DATA_DIR its label (a no-op if it has one), then commit the file
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent / "dataset_versions.json"
HASH = re.compile(r"[0-9a-f]{12}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load(path: Path | None = None) -> list[dict]:
    versions = json.loads((path or REGISTRY).read_text(encoding="utf-8"))["versions"]
    validate(versions)
    return versions


def validate(versions: list[dict]) -> None:
    """The labels are Dataset v1, v2, ... in order, and no hash appears twice."""
    seen = set()
    for number, entry in enumerate(versions, 1):
        if entry["label"] != f"Dataset v{number}":
            raise ValueError(f"entry {number} is labelled {entry['label']!r}, expected 'Dataset v{number}'")
        if not HASH.fullmatch(entry["data_version"]) or entry["data_version"] in seen:
            raise ValueError(f"entry {number} has a bad or repeated data version {entry['data_version']!r}")
        seen.add(entry["data_version"])


def label_for(data_version: str, path: Path | None = None) -> str | None:
    """The friendly name of a data version, or None when it has not been given one."""
    return next((e["label"] for e in load(path) if e["data_version"] == data_version), None)


def valid_date(value) -> bool:
    """A real calendar date written YYYY-MM-DD. Anything else is treated as no date, so a malformed snapshot date never ends up in a name."""
    if not isinstance(value, str) or DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def display_name(data_version: str, snapshot_date: str | None = None, path: Path | None = None) -> str | None:
    """The one naming rule, for manual and daily refreshes alike: a registered milestone keeps its label, anything else is "Data of YYYY-MM-DD" from the data's own snapshot date, and with
    no valid date there is no friendly name (the caller shows the hash). Nothing here ever writes the registry, so no refresh can move the counter.

    The date is the data's own snapshot date (its meta.json). It is not the day a source check ran and not the latest match date, so a check of unchanged data on a later day names it as before."""
    label = label_for(data_version, path)
    if label:
        return label
    return f"Data of {snapshot_date}" if valid_date(snapshot_date) else None


def name_or_hash(data_version: str, snapshot_date: str | None = None, path: Path | None = None) -> str:
    """The name to show for a data set: its display name, or when it has none (no valid snapshot date) its technical hash. The hash itself is unchanged and stays the identity."""
    return display_name(data_version, snapshot_date, path) or data_version


def meta_problem(meta: dict, path: Path | None = None) -> str | None:
    """Why this data folder's meta.json is not the canonical one for its version, or None. The release host keeps the FIRST data folder it received for a version and reuses it for every later release,
    so a rebuild of the same data (same hashes) must carry the registered build time. Otherwise the release's recorded /v1/meta answer (it holds the build time) would not match what the host serves."""
    entry = next((e for e in load(path) if e["data_version"] == meta["data_version"]), None)
    if entry is None:
        return None
    if meta.get("built_at_utc") != entry["built_utc"] or meta.get("snapshot_date") != entry["snapshot_date"]:
        return (f"data version {meta['data_version']} is registered as built {entry['built_utc']} (snapshot {entry['snapshot_date']}) but this folder says built {meta.get('built_at_utc')} "
                f"(snapshot {meta.get('snapshot_date')}). Same data, different metadata: run  python -m cricket_refresh.canonical_meta DATA_DIR  to carry the registered metadata")
    return None


def register(data_version: str, snapshot_date: str | None, built_utc: str | None, path: Path | None = None) -> str:
    """The label of this data version. A hash that is already registered keeps its label and nothing is written. A new hash is appended with the next number."""
    path = path or REGISTRY
    versions = load(path)
    for entry in versions:
        if entry["data_version"] == data_version:
            return entry["label"]
    label = f"Dataset v{len(versions) + 1}"
    versions.append({"label": label, "data_version": data_version, "snapshot_date": snapshot_date, "built_utc": built_utc})
    validate(versions)
    path.write_text(json.dumps({"versions": versions}, indent=1) + "\n", encoding="utf-8")
    return label


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    # The data's name is never required to exist in the registry. A milestone is registered on purpose, with the register command below.
    reg = sub.add_parser("register")
    reg.add_argument("data_dir")
    check = sub.add_parser("check", help="print the data's name and exit 1 only when its metadata disagrees with the registry. Unregistered data is named by its snapshot date")
    check.add_argument("data_dir")
    args = parser.parse_args(argv)
    if args.command == "show":
        for e in load():
            print(f"{e['label']}  {e['data_version']}  snapshot {e['snapshot_date']}  built {e['built_utc']}")
        return 0
    meta = json.loads((Path(args.data_dir) / "meta.json").read_text(encoding="utf-8"))
    if args.command == "check":
        problem = meta_problem(meta)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        name = display_name(meta["data_version"], meta.get("snapshot_date"))
        print(name or f"data version {meta['data_version']}")
        return 0
    print(register(meta["data_version"], meta.get("snapshot_date"), meta.get("built_at_utc")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
