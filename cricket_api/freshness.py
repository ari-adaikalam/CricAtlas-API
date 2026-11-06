"""Public data facts and a small, separately written refresh record.

Dataset facts are read from the verified tables at startup. Operational times are
read separately so a successful check of unchanged data needs no API restart.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from cricket_api import dataset_versions

MAX_RECORD_BYTES = 8192
EVIDENCE_WINDOW = 36 * 3600     # the same threshold as the DataRefreshStale alert
STATUSES = {"idle", "running", "prepared", "unchanged", "failed", "activated", "cache_pending", "rolled_back", "needs_recovery"}


def dataset_summary(db) -> dict:
    dates = db.query("SELECT min(d) AS first, max(d) AS latest FROM "
                     "(SELECT start_date AS d FROM matches UNION ALL SELECT end_date AS d FROM matches "
                     "UNION ALL SELECT date AS d FROM match_dates)")[0]
    values = {}
    for column in ("format", "collection", "gender", "season"):
        values[column] = [str(row["value"]) for row in db.query(
            f'SELECT DISTINCT "{column}" AS value FROM matches WHERE "{column}" IS NOT NULL ORDER BY value')]
    return {
        "dataset_label": dataset_versions.label_for(db.data_version),
        # Computed by the shared rule from the data's own snapshot date, never from a check time. The hash when there is no date. dataset_label keeps its meaning: the milestone label or null.
        "dataset_display_name": dataset_versions.name_or_hash(db.data_version, db.meta.get("snapshot_date")),
        "first_match_date": dates["first"].isoformat() if dates["first"] else None,
        "latest_match_date": dates["latest"].isoformat() if dates["latest"] else None,
        "matches": db.meta["tables"]["matches"]["rows"],
        "balls": db.meta["tables"]["balls"]["rows"],
        "players": db.meta["tables"]["players"]["rows"],
        "formats": values["format"], "collections": values["collection"],
        "genders": values["gender"], "seasons": values["season"],
    }


def utc_time(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid refresh time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("refresh times must be UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def age_seconds(value, now: datetime) -> float | None:
    """How old a recorded UTC time is, or None when it is missing, malformed, or in the future by more than a few minutes."""
    try:
        text = utc_time(value)
    except (ValueError, TypeError):
        return None
    if text is None:
        return None
    age = (now - datetime.fromisoformat(text.replace("Z", "+00:00"))).total_seconds()
    return age if age >= -300 else None


def scheduler_state(record: dict, now: datetime) -> str:
    """What the record says the daily timer is doing now: active, disabled, unknown, or unrecorded. Only a reading made within the last 36 hours counts as a
    present state. An older one says nothing about now, so it is reported as unknown. The record's historical times are separate and are never used for this."""
    scheduler = record.get("scheduler")
    if not isinstance(scheduler, dict):
        return "unrecorded"
    age = age_seconds(scheduler.get("observed_utc"), now)
    if scheduler.get("state") not in {"active", "disabled", "unknown"} or age is None or age > EVIDENCE_WINDOW:
        return "unknown"
    return scheduler["state"]


def automatic_verified(record: dict, now: datetime) -> bool:
    """Whether the page may call the refresh automatic. A record that says "automatic" is not enough. It needs, all written by the root publisher:
    the timer enabled and active in a reading made now (not carried forward), a successful check systemd tied to a run the timer started (a no-op check counts) within
    36 hours, and a publication that finished at some time (cache cleared, public check passed). History alone, however good, never makes the claim."""
    if record.get("mode") != "automatic" or scheduler_state(record, now) != "active":
        return False
    scheduled, published = age_seconds(record.get("last_scheduled_check_utc"), now), age_seconds(record.get("publication_verified_utc"), now)
    return scheduled is not None and scheduled <= EVIDENCE_WINDOW and published is not None


def refresh_record(path: Path | None, data_version: str, now: datetime | None = None) -> dict:
    result = {"record_available": False, "mode": "manual", "status": "unavailable", "automatic_verified": False, "scheduler": "unrecorded",
              "last_attempt_utc": None, "last_successful_check_utc": None,
              "last_successful_refresh_utc": None}
    if path is None:
        return result
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            return result
        record = json.loads(raw)
        if record.get("schema_version") != 1 or record.get("status") not in STATUSES:
            return result
        if record.get("mode") not in {"preparation-only", "automatic"}:
            return result
        times = {name: utc_time(record.get(name)) for name in (
            "last_attempt_utc", "last_successful_check_utc", "last_successful_refresh_utc")}
        # A rollback must not inherit the activation time of a different snapshot.
        if record.get("active_data_version") != data_version:
            times["last_successful_refresh_utc"] = None
        return {**result, **times, "record_available": True, "mode": record["mode"], "status": record["status"],
                "automatic_verified": automatic_verified(record, now or datetime.now(timezone.utc)),
                "scheduler": scheduler_state(record, now or datetime.now(timezone.utc))}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return result
