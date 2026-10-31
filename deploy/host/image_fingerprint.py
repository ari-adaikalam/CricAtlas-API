#!/usr/bin/env python3
"""A portable fingerprint of a Docker image: its filesystem layers AND the configuration that decides what runs (entrypoint, command, environment, user, working directory, ports, volumes, health check, stop
signal, labels), plus its operating system and architecture.

The layers alone are not enough: an image with the same layers and another entrypoint, environment or user is a different program. The image ID would cover all of it, but it differs between Docker's
classic and containerd image stores for the same image, so it cannot be compared between the build machine and the VPS. This is built only from content-addressed layer digests and the configuration,
which are the same on both. The build script records it in the manifest, and the host's release and rollback scripts compare it before anything runs.

  image_fingerprint.py IMAGE            print the fingerprint (needs the docker CLI; DOCKER overrides its name)
  image_fingerprint.py --stdin          read the output of `docker image inspect` from stdin
"""
import hashlib
import json
import os
import subprocess
import sys

CONFIG_KEYS = ("Entrypoint", "Cmd", "Env", "User", "WorkingDir", "ExposedPorts", "Volumes", "Healthcheck", "StopSignal", "Labels", "OnBuild", "Shell")


def normal(value):
    """An empty setting is the same as an absent one (the two image stores differ on which they write)."""
    return None if value in (None, {}, [], "") else value


def fingerprint(inspected: list) -> str:
    image = inspected[0]
    config = image.get("Config") or {}
    document = {
        "os": image.get("Os"),
        "architecture": image.get("Architecture"),
        "variant": normal(image.get("Variant")),
        "layers": image["RootFS"]["Layers"],
        "config": {key: normal(config.get(key)) for key in CONFIG_KEYS},
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    if sys.argv[1] == "--stdin":
        raw = sys.stdin.read()
    else:
        raw = subprocess.run([os.environ.get("DOCKER", "docker"), "image", "inspect", sys.argv[1]], capture_output=True, text=True, check=True).stdout
    print(fingerprint(json.loads(raw)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
