"""Download Cricsheet archives and read single matches out of them."""
import io
import re
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

from cricket_refresh import cricsheet_json
from cricket_refresh.config import CRICSHEET_JSON_URL, CRICSHEET_URL, Source, cricsheet_dir

PEOPLE_URL = "https://cricsheet.org/register/people.csv"
_MATCH_FILE = re.compile(r"^(\d+)\.csv$")


def archive_path(source: Source) -> Path:
    folder = cricsheet_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{source.archive}_csv2.zip"


def json_archive_path(source: Source) -> Path:
    return archive_path(source).with_name(f"{source.archive}_json.zip")


def download(source: Source, attempts: int = 3, json: bool = False) -> Path:
    """Fetch the archive fresh. Cricsheet corrects old matches, so a cached copy goes stale. json=True fetches the JSON archive of the same collection."""
    url = (CRICSHEET_JSON_URL if json else CRICSHEET_URL).format(archive=source.archive)
    target = json_archive_path(source) if json else archive_path(source)
    partial = target.with_suffix(".part")
    for attempt in range(1, attempts + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                with open(partial, "wb") as out:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        out.write(chunk)
            partial.replace(target)
            return target
        except requests.RequestException:
            if attempt == attempts:
                raise
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def download_people() -> Path:
    """Cricsheet's register of players, which maps its identifiers to Cricinfo player ids."""
    target = cricsheet_dir() / "people.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(PEOPLE_URL, timeout=60)
    response.raise_for_status()
    target.write_bytes(response.content)
    return target


def match_ids(archive: zipfile.ZipFile) -> set[int]:
    ids = set()
    for name in archive.namelist():
        found = _MATCH_FILE.match(name)
        if found:
            ids.add(int(found.group(1)))
    return ids


def read_match(archive: zipfile.ZipFile, match_id: int) -> tuple[pd.DataFrame, list[str]]:
    """Return the ball-by-ball rows and the raw lines of the info file for one match."""
    with archive.open(f"{match_id}.csv") as handle:
        balls = pd.read_csv(handle, dtype={"season": str, "ball": str})       # as text, so 4.10 is not read as 4.1
    with archive.open(f"{match_id}_info.csv") as handle:
        info_lines = io.TextIOWrapper(handle, encoding="utf-8").readlines()
    return balls, info_lines


def read_json_match(archive: zipfile.ZipFile, match_id: int) -> dict:
    """The JSON of one match, which holds the fielders, squads, officials, reviews and powerplays the CSV file leaves out."""
    return cricsheet_json.load(archive.read(f"{match_id}.json"))
