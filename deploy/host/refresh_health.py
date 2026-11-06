#!/usr/bin/env python3
"""Observe refresh state for node_exporter's textfile collector, without secrets.

Called by the existing ten-minute firewall check and the publisher's stop hook.
No enabled policy and no previous metrics means no change to the host.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def read(path):
    if path.is_symlink() or path.stat().st_size > 8192:
        raise ValueError("invalid refresh record")
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError("invalid refresh record")
    return result


def stamp(value):
    if value is None:
        return 0
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None or date.utcoffset() != timezone.utc.utcoffset(date):
        raise ValueError("invalid refresh timestamp")
    return date.timestamp()


# The longest deadline the worker accepts is 12 hours and systemd stops a run 5 minutes later. A run whose unit is still active is supervised and bounded by those, so the observer
# does not guess a length of its own. This limit only catches a unit that outlived both.
RUN_LIMIT_SECONDS = 12 * 3600 + 600
# After a preparation settles as prepared, the publisher starts at once. If its record has not caught up after this long, the publication did not happen.
PUBLICATION_GRACE_SECONDS = 30 * 60


def systemctl_show(unit, *properties):
    """What systemd says about a unit, or None when it cannot be read."""
    try:
        out = subprocess.run(["systemctl", "show", unit, *[f"--property={name}" for name in properties]], capture_output=True, text=True, timeout=15, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {key: value.strip() for key, separator, value in (line.partition("=") for line in out.splitlines()) if separator}


def observe(root, policy, public, output, clock=time.time, check_record=None, systemd=None, enabled_since=None):
    """systemd reads a unit's properties (see systemctl_show). enabled_since is when monitoring was switched on, in seconds. It is reported on its own and never stands in for a check."""
    if not policy.exists() and not output.exists():
        return
    enabled, failed, pending, check, refresh, committed = 1, 1, 0, 0, 0, 0
    try:
        settings = read(policy)
        armed = settings.get("enabled") is True                       # the publisher is approved to activate
        timer = None
        if systemd:
            timer = systemd("cricket-data-prepare.timer", "UnitFileState")
            if not timer or "UnitFileState" not in timer:
                raise ValueError("the timer state cannot be read")        # monitoring stays on and reports failure: a state that cannot be read never looks like nothing to report
        scheduled = bool(timer) and timer.get("UnitFileState") == "enabled"      # the owner's intent: an enabled timer that has stopped is a missed run to report, not a schedule to forget
        enabled = int(armed or scheduled)                              # a scheduled preparation is monitored even when nothing will publish it
        transaction = root / "data-activation.json"
        journal = read(transaction) if transaction.exists() else {}
        phase = journal["phase"] if transaction.exists() else None
        # When a publication that replaced the API finished its public check (journal phase committed, operation activate). The alert rules tolerate exactly one API restart in the half hour
        # after it, because the publisher recreates the API once by design. A rollback or a recovery is not counted: those are not the planned single restart.
        if phase == "committed" and journal.get("operation") == "activate":
            committed = stamp(journal.get("updated_utc"))
        if phase is not None and phase not in {"committed", "rolled_back"}:
            # Pausing the schedule must not hide an unfinished publication.
            enabled = 1
        if not enabled:
            failed = 0
        else:
            record = {}
            if armed or phase is not None:                             # a publisher exists, so its record must be there and valid
                record = read(public)
                if record.get("schema_version") != 1 or record.get("mode") != "automatic":
                    raise ValueError("no automatic refresh record")
            else:                                                      # preparation only: use the public record if there is one, and need none
                try:
                    record = read(public)
                except (OSError, ValueError):
                    record = {}
            failed = int(record.get("status") in {"failed", "needs_recovery"})
            pending = int(record.get("status") == "cache_pending")
            check = stamp(record.get("last_successful_check_utc"))
            refresh = stamp(record.get("last_successful_refresh_utc"))
            if check_record is not None and check_record.exists():
                attempt = read(check_record)
                check = max(check, stamp(attempt.get("last_successful_check_utc")))
                state = attempt.get("status")
                failed = max(failed, int(state == "failed"))
                if state == "running":
                    # healthy only while the preparation's unit is running; no unit, or one that cannot be read, is not a healthy run
                    unit = systemd("cricket-data-prepare.service", "ActiveState") if systemd else None
                    alive = bool(unit) and unit.get("ActiveState") in {"active", "activating", "deactivating"}
                    failed = max(failed, int(not alive or clock() - stamp(attempt.get("last_attempt_utc")) > RUN_LIMIT_SECONDS))
                elif armed and attempt.get("attempt_id") != record.get("attempt_id") and clock() - check_record.stat().st_mtime > PUBLICATION_GRACE_SECONDS:
                    failed = 1                                         # it settled, and the publisher never recorded it
            if phase is not None:
                pending = int(phase == "cache_pending")
                failed = max(failed, int(phase not in {"committed", "rolled_back", "cache_pending"}))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        failed = 1
    metrics = {"cricket_data_refresh_enabled": enabled, "cricket_data_refresh_failed": failed,
               "cricket_data_refresh_pending": pending, "cricket_data_refresh_last_check_seconds": check, "cricket_data_refresh_publication_committed_seconds": committed,
               "cricket_data_refresh_last_activation_seconds": refresh, "cricket_data_refresh_observed_seconds": clock(),
               # informational: when monitoring was switched on. It is never counted as a source check, and the alert rules do not use it.
               "cricket_data_refresh_monitoring_enabled_since_seconds": enabled_since or 0 if enabled else 0}
    descriptor, temporary = tempfile.mkstemp(prefix=".refresh-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            for name, value in metrics.items():
                handle.write(f"# TYPE {name} gauge\n{name} {value}\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("CRICKET_ROOT", "/srv/cricket")))
    parser.add_argument("--etc", type=Path, default=Path(os.environ.get("CRICKET_ETC", "/etc/cricket")))
    args = parser.parse_args()
    def since():
        """When monitoring was switched on: the later of the policy file's change and the timer's enablement link."""
        times = []
        for path in (args.etc / "data-refresh.json", Path("/etc/systemd/system/timers.target.wants/cricket-data-prepare.timer")):
            try:
                times.append(path.lstat().st_mtime)
            except OSError:
                pass
        return max(times) if times else None

    observe(args.root, args.etc / "data-refresh.json", Path("/srv/cricket-refresh/public/status.json"),
            Path(os.environ.get("TEXTFILE_DIR", str(args.root / "textfile"))) / "cricket_data_refresh.prom",
            check_record=Path("/srv/cricket-refresh/check/status.json"), systemd=systemctl_show, enabled_since=since())
