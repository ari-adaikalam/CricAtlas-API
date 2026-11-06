"""Where the query engine may spill to disk, and how much.

DuckDB writes intermediate results to a temporary directory when a query needs more than its memory limit. Left alone that directory is `.tmp` in the working directory, and its size cap is 90
percent of the free disk space, which on a small host is most of the disk. This module checks two settings and the database layer applies them before the configuration is locked:

  CRICKET_API_SCRATCH      a folder the process may write to, kept apart from the data. When it is not set the engine's own folder is used, and only the cap below applies.
  CRICKET_API_SCRATCH_MAX  the most the engine may keep there (default 1GiB), from 64MiB to 100GiB. It is passed as an exact number of bytes. DuckDB reads GB as 10^9 and GiB as 2^30, and so do we.

A size is read with whole-number arithmetic, never floating point: at most 32 characters (anything longer is refused before it is looked at), digits 0 to 9 with an optional decimal part, then a unit.
A size that does not come to a whole number of bytes is rounded down to the byte, which can only make the cap smaller. Spaces and tabs around the number and the unit are allowed, and nothing else is.
The result must be from 64MiB to 100GiB inclusive, so a value that is too large is refused whatever its size, and every refusal is a ConfigError.

The folder must exist and be writable, because DuckDB accepts a folder that does not exist and fails on the first spill. It must not be, hold or sit inside the data folder, which is meant to be read-only.
When the folder is known, the cap must be at most 80 percent of the size of the filesystem it is on, so that the engine refuses a spill before the volume is full. A scratch volume of a fixed
size is a deployment matter, and whether a spill, a full volume and the recovery behave is checked on the host, not by these checks.
"""
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cricket_api.settings import ConfigError, Settings

MIN_BYTES = 64 * 1024 ** 2
MAX_BYTES = 100 * 1024 ** 3
FILESYSTEM_SHARE = 0.8
UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}
MAX_INPUT = 32
SIZE = re.compile(r"[ \t]*([0-9]+)(?:\.([0-9]+))?[ \t]*([A-Za-z]+)[ \t]*", re.ASCII)       # ASCII digits and letters only, and no newline: \d and \s would take other scripts and line breaks


@dataclass(frozen=True)
class Plan:
    directory: Path | None      # None: leave the engine's own folder
    max_bytes: int

    @property
    def max_text(self) -> str:
        return f"{self.max_bytes}B"


def parse_size(text: str, name: str = "CRICKET_API_SCRATCH_MAX") -> int:
    """A size in bytes, from a number and a unit (KB MB GB TB for powers of 1000, KiB MiB GiB TiB for powers of 1024, or B).

    Exact arithmetic on integers, so no float can round a value across a limit, and a size that is not a whole number of bytes is rounded down. The input is length-limited before anything is done with
    it, and a message never repeats more than a short piece of what was given."""
    expected = f"{name} must be a size such as 1GiB or 512MiB"
    if not isinstance(text, str):
        raise ConfigError(f"{expected}, not a value of type {type(text).__name__}")
    if len(text) > MAX_INPUT:
        raise ConfigError(f"{name} is too long: a size is at most {MAX_INPUT} characters")
    match = SIZE.fullmatch(text)
    unit = UNITS.get(match.group(3).lower()) if match else None
    if match is None or unit is None:
        raise ConfigError(f"{expected}, not {text[:30]!r}")
    whole, fraction, _ = match.groups()
    fraction = fraction or ""
    size = int(whole + fraction) * unit // 10 ** len(fraction)         # the number as a whole count of 10^-len(fraction) units, times the unit, divided exactly and rounded down
    if not MIN_BYTES <= size <= MAX_BYTES:
        raise ConfigError(f"{name} must be from 64MiB to 100GiB, not {text[:30]!r}")
    return size


def human(size: int) -> str:
    return f"{size / 1024 ** 3:.1f} GiB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MiB"


def plan(settings: Settings) -> Plan:
    """Check the scratch settings and say what to apply. Raises ConfigError, so the launcher stops with a message before any data is read."""
    max_bytes = parse_size(settings.scratch_max)
    raw = settings.scratch_dir
    if raw is None:
        return Plan(None, max_bytes)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("CRICKET_API_SCRATCH is empty: give a folder, or leave it unset to use the query engine's own")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ConfigError("CRICKET_API_SCRATCH must not contain control characters")
    folder = Path(raw)
    if not folder.is_absolute():
        raise ConfigError("CRICKET_API_SCRATCH must be an absolute path")
    if not folder.exists():
        raise ConfigError(f"CRICKET_API_SCRATCH does not exist: {raw[:80]}. The query engine would only fail on its first spill, so the folder must be made first")
    if not folder.is_dir():
        raise ConfigError(f"CRICKET_API_SCRATCH is not a folder: {raw[:80]}")
    folder = folder.resolve()
    data = Path(settings.data_dir).resolve()
    if folder == data or data in folder.parents or folder in data.parents:
        raise ConfigError("CRICKET_API_SCRATCH must be apart from the data folder: the data folder is meant to be read-only, and the scratch folder is written to")
    try:
        with tempfile.NamedTemporaryFile(dir=folder):
            pass
    except OSError:
        raise ConfigError(f"CRICKET_API_SCRATCH is not writable: {raw[:80]}") from None
    total = shutil.disk_usage(folder).total
    if max_bytes > total * FILESYSTEM_SHARE:
        raise ConfigError(f"CRICKET_API_SCRATCH_MAX ({human(max_bytes)}) is more than {int(FILESYSTEM_SHARE * 100)} percent of the {human(total)} that the scratch folder's filesystem holds: "
                          "the query engine must refuse a spill before the volume is full")
    return Plan(folder, max_bytes)
