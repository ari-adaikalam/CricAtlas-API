"""The refresh result email: reading the per-attempt records and turning each into the context of one message.

The records are written by the refresh hooks (api/refresh/cricket_refresh/result.py) into a folder this container mounts read only. Everything in them is treated as untrusted text: only named
fields are read, each is checked against a pattern or a length, and anything else is ignored. A record that does not pass is skipped and counted, never partly used.

An attempt that is still waiting for its publisher after `unfinished_minutes` is reported as an unfinished publication, because "the check passed" is not "the data is live".
"""
import json
import os
import re
import stat
from datetime import datetime, timezone

HEX32 = re.compile(r"[0-9a-f]{32}")
VERSION = re.compile(r"[0-9a-f]{12}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# What the recorder may hand over as a data name: a milestone label, "Data of <date>", or the hash. The notifier never works a name out, it only shows one.
NAME = re.compile(r"Dataset v[1-9][0-9]{0,3}|Data of \d{4}-\d{2}-\d{2}|[0-9a-f]{12}")
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
PHASE = re.compile(r"[a-z_]{1,24}")
MAX_BYTES = 8192
STALE_HOURS = 48
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
OUTCOMES = {"published", "unchanged", "failed", "deferred", "timed_out", "cache_pending", "needs_recovery", "rolled_back", "publication_failed", "publication_not_recorded", "awaiting_publisher", "test"}

# outcome: (tone, subject, headline, what it means). Plain wording, and never "published" for anything that is not a finished publication.
TEXT = {
    "published": ("ok", "Refresh finished: new data is live", "New data was prepared, verified and published.", "The live API is now serving the new data."),
    "unchanged": ("ok", "Refresh finished: nothing new", "The source was checked and nothing had changed.", "The live data was not changed and nothing was published."),
    "failed": ("bad", "Refresh failed", "The preparation failed.", "Nothing was published. The live data was not changed."),
    "deferred": ("warn", "Refresh deferred: it did not start", "The run was not started because the start check was not satisfied.", "Nothing was prepared or published. The next scheduled run tries again."),
    "timed_out": ("bad", "Refresh timed out", "The preparation reached its time limit and was stopped.", "Nothing was published. The live data was not changed."),
    "cache_pending": ("bad", "Refresh needs attention: publication unfinished", "New data was switched in, but the cache clear or the public check did not finish.",
                      "This is not a completed publication. It needs the resume step in the refresh guide."),
    "needs_recovery": ("bad", "Refresh needs attention: recovery required", "The switch to new data did not finish cleanly.",
                       "Do not treat the live data as updated. It needs the recovery step in the refresh guide."),
    "rolled_back": ("warn", "Refresh rolled back to the previous data", "The new data failed its checks after the switch.", "The API was put back on the previous data."),
    "publication_failed": ("bad", "Refresh prepared data but publishing failed", "The data was prepared, but the publisher recorded a failure.", "Check which data version is live below."),
    "publication_not_recorded": ("bad", "Refresh ended with no publication record", "The preparation succeeded, but the publisher recorded no result for this attempt.",
                                 "It is not known whether new data went live. Check the version below and the freshness page."),
    # Written by hand when delivery is being checked (see the rollout plan). It says it is a test in the subject and the first line, and makes no claim about data.
    "test": ("warn", "TEST message: the refresh result email, not a real refresh", "This is a test of the refresh result email.", "No refresh ran and no data changed. Nothing needs doing."),
    "unfinished": ("bad", "Refresh publication unfinished", "The preparation succeeded, but no publication result has been recorded since.", "New data may not be live."),
}


def text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return CONTROL.sub(" ", value).strip()[:limit] or None


def parse(raw: bytes) -> dict | None:
    """The record as a clean dict, or None when it is not a valid record of this schema."""
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("key"), str) or not HEX32.fullmatch(value["key"]):
        return None
    outcome = value.get("outcome")
    if outcome not in OUTCOMES or not isinstance(value.get("final"), bool) or value["final"] != (outcome != "awaiting_publisher"):
        return None
    phases = []
    for item in value.get("phases") if isinstance(value.get("phases"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("phase"), str) and PHASE.fullmatch(item["phase"]) and isinstance(item.get("seconds"), (int, float)) and not isinstance(item["seconds"], bool):
            phases.append({"phase": item["phase"], "seconds": max(0.0, min(float(item["seconds"]), 10 ** 7))})
    pick = lambda name, pattern: value[name] if isinstance(value.get(name), str) and pattern.fullmatch(value[name]) else None
    return {"key": value["key"], "final": value["final"], "outcome": outcome, "started_utc": pick("started_utc", STAMP), "ended_utc": pick("ended_utc", STAMP),
            "phases": phases[:12], "previous_version": pick("previous_version", VERSION), "served_version": pick("served_version", VERSION),
            "previous_display_name": pick("previous_display_name", NAME), "served_display_name": pick("served_display_name", NAME),
            "data_changed": value["data_changed"] if isinstance(value.get("data_changed"), bool) else None, "latest_match_date": pick("latest_match_date", DATE),
            "reason": text(value.get("reason"), 200), "journal_phase": text(value.get("journal_phase"), 24)}


def read_records(directory: str, limit: int) -> tuple[list[tuple[float, dict]], int]:
    """Up to `limit` of the newest valid records as (modified time, record), and how many files were skipped as invalid. Only plain files named by an attempt key are opened, each at most
    8 KiB, and nothing is followed through a link."""
    found, skipped = [], 0
    try:
        entries = [e for e in os.scandir(directory) if e.name.endswith(".json") and HEX32.fullmatch(e.name[:-5])]
    except OSError:
        return [], 0
    stamped = []
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        stamped.append((info.st_mtime, info.st_size, entry))
    for mtime, size, entry in sorted(stamped, key=lambda item: item[0], reverse=True)[:limit]:
        try:
            if not stat.S_ISREG(os.lstat(entry.path).st_mode) or size > MAX_BYTES:
                skipped += 1
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(entry.path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except OSError:
            skipped += 1
            continue
        record = parse(raw) if len(raw) <= MAX_BYTES else None
        if record is None or record["key"] != entry.name[:-5]:
            skipped += 1
            continue
        found.append((mtime, record))
    return found, skipped


def hms(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours} h {minutes:02d} min" if hours else (f"{minutes} min {secs:02d} s" if minutes else f"{secs} s")


def stamp_seconds(value: str | None) -> float | None:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if value else None


def plan(record: dict, mtime: float, now: float, unfinished_seconds: float) -> tuple[str, str] | None:
    """(the outcome to report, the ledger key suffix), or None when nothing should be sent yet. A final record is reported once. One that is still waiting is reported only after the wait."""
    if now - mtime > STALE_HOURS * 3600:
        return None                                                  # a record this old is not news: it is what a lost ledger or a long outage would replay
    if record["final"]:
        return record["outcome"], record["outcome"]
    return ("unfinished", "unfinished") if now - mtime >= unfinished_seconds else None


def data_text(name: str | None, version: str | None) -> str:
    """The friendly name first and the hash alongside, "Data of 2026-10-28 (2a3579eb5b8f)". With only a hash, the hash. Without a hash, "not recorded": a name alone does not identify data."""
    if not version:
        return "not recorded"
    return f"{name} ({version})" if name and name != version else version


def context(record: dict, outcome: str, local) -> dict:
    """What the template needs. `local` formats a time in the owner's zone."""
    tone, subject, headline, meaning = TEXT[outcome]
    started, ended = stamp_seconds(record["started_utc"]), stamp_seconds(record["ended_utc"])
    reason = record["reason"]
    if outcome in ("failed", "deferred", "timed_out", "publication_not_recorded") and reason:
        meaning = f"{meaning} Recorded reason: {reason}"
    changed = {True: "yes, the live data version changed", False: "no, the live data version did not change", None: "not recorded"}[record["data_changed"]] if outcome != "unfinished" else "not known"
    rows = [("Started", local(started) if started else "not recorded"), ("Ended", local(ended) if ended else "not recorded"),
            ("Time taken", hms(ended - started) if started and ended and ended >= started else "not recorded"),
            ("Data before", data_text(record["previous_display_name"], record["previous_version"])), ("Data live now", data_text(record["served_display_name"], record["served_version"])),
            ("Data changed", changed), ("Latest match date", record["latest_match_date"] or "not recorded")]
    return {"outcome": outcome, "tone": tone, "subject_line": subject, "headline": headline, "meaning": meaning, "rows": rows,
            "phases": [{"name": p["phase"].replace("_", " "), "time": hms(p["seconds"])} for p in record["phases"]], "journal_phase": record["journal_phase"],
            "attempt": record["key"][:8]}
