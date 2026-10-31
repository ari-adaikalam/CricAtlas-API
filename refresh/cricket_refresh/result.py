"""One result record for every daily refresh attempt, for the notifier's result email. Standard library only, run by root.

Three callers:
  cricket-data-prepare.service    `ExecStopPost=-+` (after the seal), whenever a preparation ends. For a preparation that succeeded it writes the early record "awaiting_publisher", which is what lets the
                                  notifier report an unfinished publication if the publisher never produces a result. For one that failed or timed out it already writes the final outcome.
  cricket-data-activate.service   `ExecStopPost=-`: the publisher runs after every preparation (the prepare unit's OnSuccess and OnFailure), so this is where an attempt normally ends. It replaces the early
                                  record with the outcome.
  cricket-data-result.service     started by the watch's OnFailure. A deferral is the one ending neither of the above sees, because the preparation never starts.
A leading - means a fault here never changes the unit's result.

The record is built from what root can read: the builder's record, the publisher's record, the publication journal and the progress file. It is written to a folder the notifier mounts
read-only, one file per attempt, so the notifier needs no access to the refresh state and the refresh services hold no mail credential. The latest-record files are mutable and the
last-success gauge only moves on success, so neither is used to detect an attempt: each attempt leaves its own file.

A record is `final` or not. The preparation's hook runs before the publisher, so for a preparation that succeeded it can only write "awaiting_publisher". The publisher's hook then replaces that with
the outcome. A final record is never replaced by a non-final one. The notifier sends one email for a final record, and one "publication unfinished" email if a record stays non-final too long.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

CHECK = Path("/srv/cricket-refresh/check")
PUBLIC = Path("/srv/cricket-refresh/public/status.json")
WORK = Path("/srv/cricket-refresh/work")
RESULTS = Path("/srv/cricket-refresh/results")
JOURNAL = Path("/srv/cricket/data-activation.json")
DATA = Path("/srv/cricket/data")
POLICY = Path("/etc/cricket/data-refresh.json")
SCHEMA = 1
KEEP_NEWEST, KEEP_DAYS = 30, 30
MAX_JSON = 8192
MAX_REASON = 200
HEX32 = re.compile(r"[0-9a-f]{32}")
VERSION = re.compile(r"[0-9a-f]{12}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FINAL = {"published", "unchanged", "failed", "deferred", "timed_out", "cache_pending", "needs_recovery", "rolled_back", "publication_failed", "publication_not_recorded"}
NONFINAL = {"awaiting_publisher"}
# The outcomes that say the preparation itself did not finish. Whichever hook records one first keeps it: a later hook sees the same attempt a moment afterwards and may know less (the unit's
# Result property, for one), so it must not turn "timed_out" into "failed".
PREPARATION_ENDED = {"failed", "timed_out", "deferred"}
OUTCOMES = FINAL | NONFINAL
# The outcomes after which the API is serving the live release, so its latest match date can be read.
API_UP = {"published", "unchanged", "cache_pending", "rolled_back"}
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def utc(value: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read(path: Path) -> dict:
    """A small JSON object, or {} when the file is absent, a link, too large or not an object. Nothing here may stop a record from being written."""
    try:
        if path.is_symlink() or path.stat().st_size > MAX_JSON * 8:
            return {}
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def clean(text, limit: int = MAX_REASON) -> str:
    """A reason as plain bounded text. Control characters go, and so does anything that looks like a path, so a stored error never leaks a location."""
    text = CONTROL.sub(" ", str(text or "")).strip()
    text = re.sub(r"(?:/[\w.\-@+]+){2,}", "[path]", text)
    return text[:limit]


def systemd_show(unit: str, *properties: str) -> dict:
    try:
        out = subprocess.run(["systemctl", "show", unit, *[f"--property={p}" for p in properties]], capture_output=True, text=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return {key: value.strip() for key, sep, value in (line.partition("=") for line in out.splitlines()) if sep}


def name_of(version: str | None, data_root: Path) -> str | None:
    """The friendly name of a data version, by the one shared rule (cricket_api.dataset_versions): a registered milestone keeps its label, anything else is "Data of <its own snapshot date>", and with
    no readable date the hash. The date comes from that version's own meta.json, never from today's date or the time of a check, so a check of unchanged data on a later day names it as before.
    Nothing here writes the registry. None only when there is no version."""
    if not version:
        return None
    snapshot = read(data_root / version / "meta.json").get("snapshot_date") if VERSION.fullmatch(version) else None
    try:
        from cricket_api import dataset_versions
        return dataset_versions.name_or_hash(version, snapshot if isinstance(snapshot, str) else None)
    except Exception:                                  # a name is a convenience: if the rule cannot be read, the hash still identifies the data
        return version


def phases_of(progress: dict, attempt_id) -> list[dict]:
    if not attempt_id or progress.get("attempt_id") != attempt_id or not isinstance(progress.get("history"), list):
        return []
    rows = []
    for item in progress["history"][:20]:
        if isinstance(item, dict) and isinstance(item.get("phase"), str) and isinstance(item.get("seconds"), (int, float)):
            rows.append({"phase": item["phase"][:24], "seconds": round(float(item["seconds"]), 1)})
    return rows


def attempt_key(builder: dict, watch_invocation: str | None) -> str | None:
    """The attempt's identity: the builder's own id when it wrote one, else the watch's invocation, which exists for every attempt including a deferred one."""
    for candidate in (builder.get("attempt_id"), watch_invocation):
        if isinstance(candidate, str) and HEX32.fullmatch(candidate):
            return candidate
    return None


def classify(*, trigger: str, builder: dict, public: dict, prepare_result: str | None) -> tuple[str, str | None] | None:
    """(outcome, reason) for the attempt as it stands now, or None when it is still running and nothing can be said yet."""
    status, stopped = builder.get("status"), clean(builder.get("stopped_reason"))
    if status == "failed" and stopped.startswith("deferred"):
        return "deferred", stopped
    if prepare_result == "timeout" or (status == "failed" and "deadline" in stopped.lower()):
        return "timed_out", stopped or "The preparation reached its time limit."
    if status == "failed":
        return "failed", stopped or "The preparation failed. The private journal has the detail."
    if status not in ("prepared", "unchanged"):
        return None
    attempt = builder.get("attempt_id")
    if trigger in ("prepare", "watch"):
        return "awaiting_publisher", None
    if attempt and public.get("attempt_id") == attempt:
        outcome = {"activated": "published", "unchanged": "unchanged", "cache_pending": "cache_pending", "needs_recovery": "needs_recovery",
                   "rolled_back": "rolled_back", "failed": "publication_failed"}.get(public.get("status"))
        if outcome:
            return outcome, None
    return "publication_not_recorded", "The publisher ended without recording this attempt."


def latest_match_date(hostname: str | None, fetch=None) -> str | None:
    """The newest match date the live API reports, or None when it cannot be read. One short request, no redirects, never an error. `hostname` is a host name or the public address (https://host/api)."""
    if not hostname or not re.fullmatch(r"(?:https://)?[a-z0-9.-]{1,253}(?:/[a-z0-9._-]{1,32})?", hostname):
        return None
    try:
        if fetch is None:
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            with build_opener(NoRedirect).open(Request(f"{hostname if hostname.startswith('https://') else 'https://' + hostname}/v1/meta", headers={"User-Agent": "cricatlas-refresh-result/1"}), timeout=8) as response:
                body = response.read(1024 * 1024 + 1)
            value = json.loads(body) if len(body) <= 1024 * 1024 else {}
        else:
            value = fetch(hostname)
        # The live endpoint wraps the document as {"data": {...}}.
        document = value.get("data") if isinstance(value.get("data"), dict) else value
        date = document.get("coverage", {}).get("latest_match_date")
    except Exception:
        return None
    return date if isinstance(date, str) and DATE.fullmatch(date) else None


def build(*, trigger: str, builder: dict, public: dict, journal: dict, progress: dict, prepared: dict, watch_invocation: str | None, prepare_result: str | None,
          hostname: str | None = None, fetch=None, data_root: Path = DATA, clock=time.time) -> dict | None:
    key = attempt_key(builder, watch_invocation)
    verdict = classify(trigger=trigger, builder=builder, public=public, prepare_result=prepare_result)
    if key is None or verdict is None:
        return None
    outcome, reason = verdict
    mine = prepared.get("attempt_id") == builder.get("attempt_id") and builder.get("attempt_id") is not None
    served = public.get("active_data_version")
    previous = prepared.get("baseline_data_version") if mine else None
    served = served if isinstance(served, str) and VERSION.fullmatch(served) else None
    previous = previous if isinstance(previous, str) and VERSION.fullmatch(previous) else None
    if outcome == "unchanged" and previous is None:
        previous = served                                            # nothing was published, so the data before is the data live now
    started = progress.get("started_utc") if progress.get("attempt_id") == builder.get("attempt_id") else builder.get("last_attempt_utc")
    record = {"schema_version": SCHEMA, "key": key, "final": outcome in FINAL, "outcome": outcome,
              "started_utc": started if isinstance(started, str) and len(started) <= 25 else None, "ended_utc": utc(clock()), "recorded_utc": utc(clock()),
              "phases": phases_of(progress, builder.get("attempt_id")), "previous_version": previous, "served_version": served,
              "previous_display_name": name_of(previous, data_root), "served_display_name": name_of(served, data_root),
              "data_changed": (previous != served) if previous and served and outcome in ("published", "rolled_back") else (False if outcome == "unchanged" else None),
              "latest_match_date": latest_match_date(hostname, fetch) if outcome in API_UP else None, "reason": reason,
              "journal_phase": journal.get("phase") if isinstance(journal.get("phase"), str) else None}
    if record["journal_phase"]:
        record["journal_phase"] = clean(record["journal_phase"], 24)
    return record


def write(results: Path, record: dict) -> str:
    """Write the record by an atomic replace. A final record already there is kept unless this one is also final (a later, more complete account of the same attempt). Returns what was done."""
    results.mkdir(mode=0o755, parents=True, exist_ok=True)
    target = results / f"{record['key']}.json"
    existing = read(target)
    if existing.get("final") and not record["final"]:
        return "kept: a final record exists"
    if existing.get("final") and existing.get("outcome") == record["outcome"] and existing.get("started_utc") == record["started_utc"]:
        return "kept: the same outcome is recorded"
    if existing.get("final") and existing.get("outcome") in PREPARATION_ENDED and record["outcome"] in PREPARATION_ENDED:
        return "kept: the first account of how the preparation ended"
    descriptor, name = tempfile.mkstemp(prefix=".result-", dir=results)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return "written"


def prune(results: Path, clock=time.time) -> int:
    """Keep the newest few and anything recent, so the folder stays small however long the schedule runs."""
    files = sorted((p for p in results.glob("*.json") if HEX32.fullmatch(p.stem)), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for path in files[KEEP_NEWEST:]:
        if clock() - path.stat().st_mtime > KEEP_DAYS * 86400:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def record_attempt(trigger: str, *, check: Path = CHECK, public: Path = PUBLIC, work: Path = WORK, results: Path = RESULTS, journal: Path = JOURNAL, policy: Path = POLICY,
                   environment=None, show=systemd_show, fetch=None, data_root: Path = DATA, clock=time.time) -> str:
    environment = os.environ if environment is None else environment
    builder = read(check / "status.json")
    # The watch runs once for every attempt, a deferred one included, so its invocation names an attempt that has no builder id. systemd keeps it for the unit's latest run after it has gone.
    watch_invocation = show("cricket-data-watch.service", "InvocationID").get("InvocationID")
    # While the preparation's own stop hook runs its Result property is not final yet, so systemd hands the result to the hook in the environment.
    prepare_result = environment.get("SERVICE_RESULT") if trigger == "prepare" else show("cricket-data-prepare.service", "Result").get("Result")
    record = build(trigger=trigger, builder=builder, public=read(public), journal=read(journal), progress=read(check / "progress.json"), prepared=read(work / "prepared.json"),
                   watch_invocation=watch_invocation, prepare_result=prepare_result, hostname=read(policy).get("public_base") or read(policy).get("hostname"), fetch=fetch, data_root=data_root, clock=clock)
    if record is None:
        return "nothing to record yet"
    done = write(results, record)
    prune(results, clock)
    return f"{record['outcome']}: {done}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--record", choices=("prepare", "watch", "activate"), required=True)
    args = parser.parse_args(argv)
    try:
        print(record_attempt(args.record))
    except Exception as error:                      # a hook must never fail a unit, and never print what was in the records
        print(f"result record not written ({type(error).__name__})", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
