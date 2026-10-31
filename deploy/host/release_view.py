#!/usr/bin/env python3
"""Read-only comparison view of an installed release, without image archives.

The Mac uses this over strictly verified Tailscale SSH when a data-only release
was created on the VPS. No rendered notifier config or secret is returned.
"""
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 ** 2), b""):
            result.update(block)
    return result.hexdigest()


def config_files(folder):
    result = {}
    for path in sorted(folder.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("config contains a link or special file")
        if stat.S_ISREG(info.st_mode):
            result[str(path.relative_to(folder))] = {"sha256": checksum(path), "mode": stat.S_IMODE(info.st_mode)}
    return result


def view(root, expected):
    if re.fullmatch(r"[A-Za-z0-9._-]{8,80}", expected) is None:
        raise ValueError("invalid release ID")
    release = (root / "releases/current").resolve(strict=True)
    if release.parent != root / "releases" or release.name != expected:
        raise ValueError("live release changed before comparison")
    manifest = json.loads((release / "manifest.json").read_text())
    if manifest["release_id"] != expected:
        raise ValueError("release identity differs")
    version = manifest["data"]["version"]
    if re.fullmatch(r"[0-9a-f]{12}", version) is None:
        raise ValueError("invalid data version")
    data = root / "data" / version
    if data.is_symlink() or checksum(data / "meta.json") != manifest["data"]["meta_sha256"]:
        raise ValueError("installed metadata differs")
    meta = json.loads((data / "meta.json").read_text())
    actual = {}
    for table in meta["tables"].values():
        name = table["file"]
        if re.fullmatch(r"[a-z][a-z0-9_]*\.parquet", name) is None or (data / name).is_symlink():
            raise ValueError("invalid table path")
        actual[name] = checksum(data / name)
        if actual[name] != table["sha256"]:
            raise ValueError("installed table differs")
    if actual != manifest["data"]["tables"] or set(p.name for p in data.iterdir()) != {"meta.json", *actual}:
        raise ValueError("installed table set differs")
    config = config_files(release / "config")
    actual["meta.json"] = checksum(data / "meta.json")
    if (root / "releases/current").resolve() != release:
        raise ValueError("live release changed during comparison")
    return {"schema_version": 1, "manifest": manifest, "config_files": config, "data_files": actual}


if __name__ == "__main__":
    print(json.dumps(view(Path(os.environ.get("CRICKET_ROOT", "/srv/cricket")), sys.argv[1])))
