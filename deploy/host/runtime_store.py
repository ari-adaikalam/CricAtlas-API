#!/usr/bin/env python3
"""An opt-in, content-addressed config store for data-only releases.

Run after rendering a full release, before switching it. Existing deployments
keep release-local paths unless the owner explicitly requests shared configs.
Installed release references determine retention. Unknown folders stay put.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def digest(folder):
    entries = []
    for path in sorted(folder.rglob("*")):
        if path.name == ".runtime-store.json" and path.parent == folder:
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("runtime config contains a link or special file")
        entries.append((str(path.relative_to(folder)), stat.S_IMODE(mode),
                        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def validate(folder, root):
    parent = root / "runtime-configs"
    if folder.is_symlink() or folder.parent != parent or re.fullmatch(r"[0-9a-f]{64}", folder.name) is None:
        raise ValueError("not a shared runtime config")
    marker = json.loads((folder / ".runtime-store.json").read_text())
    if marker != {"sha256": folder.name} or digest(folder) != folder.name:
        raise ValueError("shared runtime config differs from its recorded hash")
    return folder


def install(release, root):
    if (release / "config").is_symlink():
        return validate((release / "config").resolve().parent, root)
    parent = root / "runtime-configs"
    parent.mkdir(mode=0o755, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".config-", dir=parent) as temporary:
        stage = Path(temporary)
        # Reject links before copytree follows anything.
        digest(release / "config")
        if (release / "notifier.toml").is_symlink():
            raise ValueError("notifier config must be a regular file")
        shutil.copytree(release / "config", stage / "config")
        shutil.copy2(release / "notifier.toml", stage / "notifier.toml")
        stage.chmod(0o755)
        key = digest(stage)
        target = parent / key
        if target.exists():
            validate(target, root)
        else:
            (stage / ".runtime-store.json").write_text(json.dumps({"sha256": key}) + "\n")
            os.rename(stage, target)
            # TemporaryDirectory may remove a missing path on exit.
        shutil.rmtree(release / "config")
        (release / "notifier.toml").unlink()
        (release / "config").symlink_to(target / "config", target_is_directory=True)
        (release / "notifier.toml").symlink_to(target / "notifier.toml")
        env = release / "release.env"
        lines = env.read_text().splitlines()
        replacements = {"CONFIG_DIR": str(target / "config"), "NOTIFIER_CONFIG_FILE": str(target / "notifier.toml")}
        env.write_text("\n".join(f"{key}={replacements[key]}" if (key := line.partition("=")[0]) in replacements else line
                                 for line in lines) + "\n")
    return target


def prune(root):
    parent = root / "runtime-configs"
    if not parent.is_dir():
        return
    keep = set()
    for release in (root / "releases").iterdir():
        if release.is_symlink() or not release.is_dir() or not (release / "manifest.json").is_file():
            continue
        if (release / "config").is_symlink():
            folder = (release / "config").resolve().parent
            validate(folder, root)
            keep.add(folder.name)
    for folder in parent.iterdir():
        if folder.is_symlink() or not folder.is_dir() or folder.name in keep:
            continue
        if re.fullmatch(r"[0-9a-f]{64}", folder.name) and (folder / ".runtime-store.json").is_file():
            validate(folder, root)
            shutil.rmtree(folder)


if __name__ == "__main__":
    action, root, *args = sys.argv[1:]
    root = Path(root)
    if action == "install":
        print(install(Path(args[0]), root))
    elif action == "prune":
        prune(root)
    else:
        raise SystemExit("expected install or prune")
