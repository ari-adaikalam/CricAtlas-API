"""The installed code and dependency bundle of the daily refresh: what it records and how it is checked.

A bundle is built on the Mac (deploy/local/build_refresh_bundle.py), sent over Tailscale and installed by root (deploy/host/install_refresh_host.sh).
BUNDLE.json lists every code file and every wheel with its SHA-256, the interpreter it was locked for and the pinned versions. Its own SHA-256 is
the bundle's identity. The root publisher's policy names that identity, so installing different code means reviewing the policy again.

Standard library only: the check must run before anything in the virtual environment is trusted.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import stat
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path("/opt/cricket-refresh")
SCHEMA = 1
IDENTITY = re.compile(r"[0-9a-f]{64}")
BUNDLE_ID = re.compile(r"\d{8}-[0-9a-f]{8}-[0-9a-f]{8}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value: dict) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True) + "\n").encode()


def tag_of_interpreter() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def tree_hashes(folder: Path) -> dict:
    """Every regular file under the folder, as a relative path and a hash. A link or a special file is refused."""
    found = {}
    for path in sorted(folder.rglob("*")):
        if path.name == "__pycache__" or "__pycache__" in path.parts:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("the code tree holds a link or a special file")
        found[path.relative_to(folder).as_posix()] = sha256_file(path)
    return found


def lock_packages(requirements: Path) -> dict:
    return {m.group(1).lower().replace("_", "-"): m.group(2)
            for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", requirements.read_text(), re.M)}


def verify(root: Path = ROOT, expected_identity: str | None = None) -> dict:
    """Raise ValueError naming what differs. Returns the bundle record and its identity when every check passes."""
    pointer = root / "BUNDLE.json"
    if not pointer.is_symlink():
        raise ValueError("no installed bundle")
    record_path = pointer.resolve(strict=True)
    folder = record_path.parent
    if folder.parent != (root / "bundles").resolve() or BUNDLE_ID.fullmatch(folder.name) is None:
        raise ValueError("the bundle record is outside the bundle store")
    identity = sha256_file(record_path)
    if expected_identity is not None and (IDENTITY.fullmatch(expected_identity) is None or identity != expected_identity):
        raise ValueError("the installed bundle is not the one the policy approves")
    record = json.loads(record_path.read_text())
    if record.get("schema_version") != SCHEMA or record.get("bundle_id") != folder.name:
        raise ValueError("the bundle record does not match its folder")
    if record.get("dirty"):
        raise ValueError("a bundle built from uncommitted files is not installable for the daily refresh")
    for link, target in (("api", folder / "api"), ("venv", folder / "venv")):
        if not (root / link).is_symlink() or (root / link).resolve(strict=True) != target.resolve(strict=True):
            raise ValueError(f"/opt link {link} does not point at the recorded bundle")
    if tree_hashes(folder / "api") != record["code"]:
        raise ValueError("the installed code differs from its record")
    if record["python_tag"] != tag_of_interpreter() or platform.machine() != record["machine"]:
        raise ValueError("the interpreter or machine differs from the one the bundle was locked for")
    for name, version in record["packages"].items():
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            raise ValueError(f"package {name} is not installed") from None
        if installed != version:
            raise ValueError(f"package {name} is {installed}, the lock says {version}")
    return {"bundle_id": record["bundle_id"], "identity": identity}


def verify_stage(stage: Path, expected_identity: str) -> dict:
    """Check a bundle folder that has been sent to the host but not installed: its record, code, wheels and lock agree with each other."""
    identity = sha256_file(stage / "BUNDLE.json")
    if IDENTITY.fullmatch(expected_identity) is None or identity != expected_identity:
        raise ValueError("the staged bundle is not the one that was built")
    description = json.loads((stage / "BUNDLE.json").read_text())
    if description.get("schema_version") != SCHEMA or BUNDLE_ID.fullmatch(description.get("bundle_id", "")) is None or description.get("dirty"):
        raise ValueError("the bundle record is not valid for installation")
    if sha256_file(stage / "requirements.txt") != description["lock_sha256"]:
        raise ValueError("the staged lock differs from the record")
    if tree_hashes(stage / "code" / "api") != description["code"]:
        raise ValueError("the staged code differs from the record")
    wheels = {path.name: sha256_file(path) for path in sorted((stage / "wheelhouse").iterdir())}
    if wheels != description["wheels"]:
        raise ValueError("the staged wheels differ from the record")
    lock = lock_packages(stage / "requirements.txt")
    if lock != description["packages"]:
        raise ValueError("the recorded versions differ from the lock")
    return {"bundle_id": description["bundle_id"], "python_tag": description["python_tag"], "machine": description["machine"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--verify-stage", type=Path, help="check a staged, not yet installed bundle folder (needs --identity)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--identity", help="the SHA-256 the root policy approves")
    args = parser.parse_args(argv)
    if not args.verify and not args.verify_stage:
        print("Plan only. --verify checks the installed bundle against its record and, with --identity, against the policy. --verify-stage checks a staged bundle.")
        return 0
    try:
        result = verify_stage(args.verify_stage, args.identity or "") if args.verify_stage else verify(args.root, args.identity)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        print(f"bundle check failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
