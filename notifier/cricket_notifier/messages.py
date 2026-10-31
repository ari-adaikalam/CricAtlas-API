"""What goes into an alert, a recovery, a digest and a notice: the context for the templates, built from sanitized, bounded parts.

An Alertmanager webhook is data from a sender that is trusted to be Alertmanager but not to be small or clean. Only an allow list of labels and annotations is copied, each cut to a fixed length with
control characters removed, at most 10 alerts are shown (the rest are counted), and everything is escaped by the template engine. So the largest alert message is bounded by construction, and the
renderer still enforces its size limit as a second guard.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

MAX_SHOWN = 10
MAX_CHARS = 120
MAX_NAMES = 10
LABELS = ("alertname", "severity")
ANNOTATIONS = ("summary", "description")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def clean(value, limit: int = MAX_CHARS) -> str:
    """Text from outside, reduced: control characters and line breaks become one space, and it is cut to `limit` characters."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = CONTROL.sub(" ", str(value)).strip()
    return text[:limit]


def parse_time(value) -> datetime | None:
    """An Alertmanager timestamp (RFC 3339), or None. A year 1 timestamp is Alertmanager's way of saying not set."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,9})?(Z|[+-]\d\d:\d\d)", value):
        return None
    try:
        parsed = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00")))
    except ValueError:
        return None
    return None if parsed.year <= 1 else parsed.astimezone(timezone.utc)


def duration_text(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    s = int(seconds)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} h"


def shown_time(moment: datetime | None, zone) -> str | None:
    from cricket_notifier.schedule import describe
    return None if moment is None else describe(moment, zone)["local"] + ", " + describe(moment, zone)["utc"]


def alert_context(webhook: dict, zone) -> dict:
    """The context for an alert (status firing) or a recovery (status resolved) from one Alertmanager webhook that has been checked (see webhook.py)."""
    status = "resolved" if webhook.get("status") == "resolved" else "firing"
    alerts = webhook.get("alerts", [])
    rows, starts, ends = [], [], []
    for alert in alerts:
        labels, notes = alert.get("labels", {}), alert.get("annotations", {})
        started, ended = parse_time(alert.get("startsAt")), parse_time(alert.get("endsAt"))
        starts.append(started)
        ends.append(ended)
        rows.append({"name": clean(labels.get("alertname", "unnamed"), 60) or "unnamed", "severity": clean(labels.get("severity", ""), 20), "summary": clean(notes.get("summary", "")),
                     "description": clean(notes.get("description", "")), "state": "resolved" if alert.get("status") == "resolved" else "firing", "started": shown_time(started, zone),
                     "ended": shown_time(ended, zone) if status == "resolved" else None})
    earliest = min((t for t in starts if t), default=None)
    latest_end = max((t for t in ends if t), default=None)
    seconds = (latest_end - earliest).total_seconds() if earliest and latest_end and status == "resolved" else None
    names = sorted({r["name"] for r in rows})
    return {"status": status, "count": len(rows), "alerts": rows[:MAX_SHOWN], "more": max(len(rows) - MAX_SHOWN, 0), "names": names[:MAX_NAMES], "more_names": max(len(names) - MAX_NAMES, 0),
            "started": shown_time(earliest, zone), "ended": shown_time(latest_end, zone) if status == "resolved" else None, "duration": duration_text(seconds),
            "group_name": clean(webhook.get("commonLabels", {}).get("alertname", names[0] if names else "alert"), 80) or "alert"}


def alert_key(webhook: dict, reminder_bucket: int) -> str:
    """One webhook is one message, however often it is delivered: a hash of the group, the status, each alert's fingerprint and times, and the reminder bucket (so reminders are at most one per bucket)."""
    status = "resolved" if webhook.get("status") == "resolved" else "firing"
    parts = sorted(f"{clean(a.get('fingerprint', ''), 64)}|{clean(a.get('startsAt', ''), 40)}|{clean(a.get('endsAt', ''), 40) if status == 'resolved' else ''}" for a in webhook.get("alerts", []))
    material = json.dumps([clean(webhook.get("groupKey", ""), 300), status, parts, reminder_bucket], separators=(",", ":"))
    return f"{'recovery' if status == 'resolved' else 'alert'}/{hashlib.sha256(material.encode()).hexdigest()[:40]}"


def digest_context(held: list, still_firing: list, opened: str | None, closed: str | None, zone, dropped: int = 0) -> dict:
    """What a breaker that has closed reports: the alert and recovery messages that were held, counted, with the names that were firing and the ones that resolved."""
    def names_of(kind):
        seen = {}
        for item in held:
            if item["kind"] == kind:
                name = clean(item.get("context", {}).get("group_name") or item.get("context", {}).get("alertname") or "alert", 80)
                seen[name] = seen.get(name, 0) + 1
        return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
    fired, resolved = names_of("alert"), names_of("recovery")
    return {"held": len(held), "fired": fired[:MAX_NAMES], "more_fired": max(len(fired) - MAX_NAMES, 0), "resolved": resolved[:MAX_NAMES], "more_resolved": max(len(resolved) - MAX_NAMES, 0),
            "still_firing": [clean(n, 80) for n in still_firing[:MAX_NAMES]], "more_still": max(len(still_firing) - MAX_NAMES, 0), "opened": opened, "closed": closed, "dropped": int(dropped)}


def alert_stored_context(webhook_context: dict) -> dict:
    """What the ledger keeps of an alert for a later digest: names and counts only, a few hundred bytes."""
    return {"group_name": webhook_context["group_name"], "status": webhook_context["status"], "count": webhook_context["count"], "names": webhook_context["names"][:5]}
