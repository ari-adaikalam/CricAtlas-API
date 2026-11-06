"""Give an export of an already registered data version the registered metadata.

    python -m cricket_refresh.canonical_meta DATA_DIR

A data version is a hash of the table files, so the same version means the same Parquet bytes. A rebuild writes a new `built_at_utc` into meta.json all the same. The release host keeps the first data
folder it got for a version and reuses it, and a release's recorded /v1/meta answer includes the build time, so the rebuild's metadata would make the release's smoke check fail and roll it back.
This sets built_at_utc to the registered value in the export folder (only there: never in a release folder, never on the host). It refuses when the snapshot date differs, because that is not the same data.
For a version that is not registered it does nothing.
"""
import json
import os
import sys
from pathlib import Path

from cricket_refresh.config import API_ROOT

REGISTRY = API_ROOT / "cricket_api" / "dataset_versions.json"


def canonicalise(folder: Path, registry: Path = REGISTRY) -> str:
    """Returns "unregistered", "already canonical" or "set to the registered build time". Raises ValueError when the data is not the registered data."""
    meta_path = folder / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    versions = json.loads(registry.read_text(encoding="utf-8"))["versions"]
    entry = next((e for e in versions if e["data_version"] == meta["data_version"]), None)
    if entry is None:
        return "unregistered"
    if meta.get("snapshot_date") != entry["snapshot_date"]:
        raise ValueError(f"data version {meta['data_version']} is registered with snapshot {entry['snapshot_date']} but this folder has {meta.get('snapshot_date')}: not the same data")
    if meta.get("built_at_utc") == entry["built_utc"]:
        return "already canonical"
    meta["built_at_utc"] = entry["built_utc"]
    temp = meta_path.with_suffix(".tmp")
    temp.write_text(json.dumps(meta, indent=1), encoding="utf-8")          # the same layout the export writes
    os.replace(temp, meta_path)
    return "set to the registered build time"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    try:
        print(canonicalise(Path(args[0])))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
