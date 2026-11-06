"""Paths and the Cricsheet archives the API data is built from. Everything lives under api/data/ (gitignored)."""
import os
from dataclasses import dataclass
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]            # the api/ folder

CRICSHEET_URL = "https://cricsheet.org/downloads/{archive}_csv2.zip"
CRICSHEET_JSON_URL = "https://cricsheet.org/downloads/{archive}_json.zip"


@dataclass(frozen=True)
class Source:
    archive: str  # Cricsheet archive name, without the _csv2.zip suffix
    format: str
    type: str


# One archive per slice of the data. Every match in these archives is kept. The list and the labels are those of the dataset the live data was built from.
SOURCES = (
    Source("tests", "TEST", "Intl"),
    Source("odis", "ODI", "Intl"),
    Source("t20s", "T20", "Intl"),
    Source("ipl", "T20", "Fran(IPL)"),
    Source("wpl", "T20", "Fran(WPL)"),
    Source("bbl", "T20", "Fran(BBL)"),
    Source("wbb", "T20", "Fran(WBBL)"),
    Source("cpl", "T20", "Fran(CPL)"),
    Source("psl", "T20", "Fran(PSL)"),
    Source("sat", "T20", "Fran(SA20)"),
    Source("ilt", "T20", "Fran(ILT20)"),
    Source("hnd", "T20", "Fran(Hundred)"),
    Source("mlc", "T20", "Fran(MLC)"),
    Source("lpl", "T20", "Fran(LPL)"),
    Source("bpl", "T20", "Fran(BPL)"),
)


def data_dir() -> Path:
    return Path(os.environ.get("CRICKET_API_DATA_ROOT") or API_ROOT / "data")


def inputs_dir() -> Path:
    """Downloaded Cricsheet archives, the people register and the frozen player profile snapshot."""
    path = data_dir() / "inputs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cricsheet_dir() -> Path:
    path = inputs_dir() / "cricsheet"
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_dir() -> Path:
    """Per-archive row files, the intermediate step between the archives and the CSV files the export reads."""
    path = data_dir() / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def csv_dir() -> Path:
    """The CSV files the export reads (the same names and columns as the dataset's, without any Cricinfo column)."""
    path = data_dir() / "csv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profiles_dir() -> Path:
    """Every imported player profile snapshot, one folder each. Nothing here is ever overwritten or deleted by the code."""
    path = inputs_dir() / "profiles"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)        # the snapshots hold exact birth dates: private to this user
    return path


def profile_snapshot() -> Path:
    """The CSV of the latest successfully imported snapshot, named by profiles/latest.json. A refresh never reads anything else for profiles."""
    import json
    import re

    pointer = profiles_dir() / "latest.json"
    if not pointer.exists():
        raise FileNotFoundError("no player profile snapshot has been imported yet: run api/import_player_profiles.command")
    name = json.loads(pointer.read_text(encoding="utf-8"))["snapshot"]
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", str(name)):
        raise ValueError(f"{pointer} names a snapshot that is not a snapshot folder name: {name!r}")          # never a path
    return profiles_dir() / name / "player_profiles.csv"
