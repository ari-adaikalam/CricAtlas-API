"""What the notifier watches about itself, and the notices it raises.

A failed email never causes more failed emails. So the conditions here are computed from the ledger (never from "no email lately", because the only message expected on an ordinary day is the daily
summary), a notice has a fixed identity per episode (a thousand late messages are one notice), and the notices about email itself are exempt from the conditions: a failure to send one is recorded and
raises nothing.

  email path   a scheduled summary is overdue, a message is past its deadline, the number of failed or unconfirmed messages has gone up, or the circuit breaker is open. One notice when it begins and one
               when it ends. The first is the probe the breaker sends when its cool-down is over.
  host         free disk space below the critical level, read directly from the filesystem so it does not depend on Prometheus being able to write. One notice when it falls and one when it recovers.
  suppression  alerts were held back by the day's budget: one notice a day, from the pool's reserved last slots.
  digest       the breaker closed: the alert and recovery mail held while it was open is replaced by one message.
"""
import logging
import shutil
from datetime import datetime, timezone

from cricket_notifier import messages, refresh_results
from cricket_notifier.config import Config
from cricket_notifier.ledger import Ledger, StorageRefused, utc_day
from cricket_notifier.errors import StorageUnavailable
from cricket_notifier.render import Renderer
from cricket_notifier.schedule import describe

log = logging.getLogger("cricket_notifier.watcher")
NOTICE_HOURS = 6                 # a notice that has not gone out in this long is expired
HOUR = 3600


def human_bytes(n: float) -> str:
    for unit, size in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if n >= size:
            return f"{n / size:.2f} {unit}"
    return f"{int(n)} B"


class Watcher:
    def __init__(self, ledger: Ledger, renderer: Renderer, config: Config, free_space=None):
        self.ledger, self.renderer, self.config = ledger, renderer, config
        self.free_space = free_space or shutil.disk_usage
        self.zone = config.schedule.zone
        self.refresh_seen: dict = {}
        self.refresh_skipped = 0

    def _notice(self, kind: str, key: str, context: dict, now: float) -> str:
        """Store a notice: "created", "exists" (the key was stored before, which changes nothing) or "failed". A failure to store is logged and does not raise, because a notice about trouble must not
        itself become trouble, and the caller leaves the episode unmarked so the notice is tried again."""
        try:
            _, body = self.renderer.message(kind, context)
            return "created" if self.ledger.persist(kind=kind, key=key, body=body, now=now, deadline=now + NOTICE_HOURS * HOUR).created else "exists"
        except (StorageRefused, StorageUnavailable):
            log.error("a %s notice could not be stored", kind)
            return "failed"

    def _local(self, ts: float) -> str:
        return describe(datetime.fromtimestamp(ts, timezone.utc), self.zone)["local"]

    # -- the email path --------------------------------------------------------------------------------------------------------------
    def conditions(self, now: float) -> tuple[list[str], dict]:
        report = self.ledger.overdue_report(now, self.config.schedule.grace_minutes * 60)
        seen = self.ledger.get_meta("failed_seen", 0)
        found = []
        if report["summary_overdue_seconds"] > 0:
            found.append(f"A scheduled summary is overdue by {int(report['summary_overdue_seconds'] // 60)} minutes.")
        if report["oldest_overshoot_seconds"] > 0:
            found.append(f"A message is {int(report['oldest_overshoot_seconds'] // 60)} minutes past its deadline.")
        if report["failed_or_unconfirmed"] > seen:
            found.append(f"Failed or unconfirmed messages rose from {seen} to {report['failed_or_unconfirmed']}.")
        breaker = self.ledger.breaker()
        if breaker.get("state", "closed") != "closed":
            found.append("The circuit breaker is open, so alert mail is held.")
        if report["failed_or_unconfirmed"] != seen:
            self.ledger.set_meta("failed_seen", report["failed_or_unconfirmed"])
        return found, report

    def check_email_path(self, now: float) -> str | None:
        """Raise the one notice when the email path starts failing and the one when it recovers. Returns 'failing', 'recovered' or None."""
        found, report = self.conditions(now)
        state = self.ledger.get_meta("email_path", {"open": False, "since": 0, "episode": 0, "quiet_since": None})
        quiet = self.config.schedule.grace_minutes * 60
        if found and state["open"] and state.get("quiet_since") is not None:
            self.ledger.set_meta("email_path", {**state, "quiet_since": None})                  # it is not over: the clock for ending it starts again
            return None
        if found and not state["open"]:
            episode = state["episode"] + 1
            context = {"state": "failing", "conditions": found[:6], "failed": report["failed_or_unconfirmed"], "unconfirmed": self.ledger.read_one("SELECT COUNT(*) FROM messages WHERE state='unconfirmed'")[0],
                       "breaker": self.ledger.breaker().get("state", "closed"), "since": self._local(now), "duration": None}
            if self._notice("email_path", f"email_path/failing/{episode}", context, now) == "failed":
                return None
            self.ledger.set_meta("email_path", {"open": True, "since": now, "episode": episode, "quiet_since": None})
            return "failing"
        if not found and state["open"] and state.get("quiet_since") is None:
            self.ledger.set_meta("email_path", {**state, "quiet_since": now})                   # nothing is wrong now, but an episode ends only after a quiet period, so a flicker is not two notices
            return None
        if not found and state["open"] and now - state["quiet_since"] >= quiet:
            context = {"state": "recovered", "conditions": [], "failed": 0, "unconfirmed": 0, "breaker": "closed", "since": None, "duration": messages.duration_text(now - state["since"])}
            if self._notice("email_path", f"email_path/recovered/{state['episode']}", context, now) == "failed":
                return None
            self.ledger.set_meta("email_path", {"open": False, "since": state["since"], "episode": state["episode"], "quiet_since": None})
            return "recovered"
        return None

    # -- the disk --------------------------------------------------------------------------------------------------------------------
    def check_disk(self, now: float) -> str | None:
        d = self.config.disk
        try:
            usage = self.free_space(d.path)
        except OSError:
            log.error("the disk could not be read")
            return None
        percent = 100 * usage.free / usage.total
        state = self.ledger.get_meta("host", {"low": False, "since": 0, "episode": 0})
        context = {"free_percent": round(percent, 1), "free": human_bytes(usage.free), "total": human_bytes(usage.total), "path": d.path}
        if percent < d.critical_free_percent and not state["low"]:
            episode = state["episode"] + 1
            if self._notice("host", f"host/low/{episode}", {"state": "low", **context}, now) == "failed":
                return None
            self.ledger.set_meta("host", {"low": True, "since": now, "episode": episode})
            return "low"
        if percent >= d.recover_free_percent and state["low"]:
            if self._notice("host", f"host/recovered/{state['episode']}", {"state": "recovered", **context}, now) == "failed":
                return None
            self.ledger.set_meta("host", {"low": False, "since": state["since"], "episode": state["episode"]})
            return "recovered"
        return None

    # -- the budget ----------------------------------------------------------------------------------------------------------------
    def flush_suppression(self, now: float) -> bool:
        """One notice a UTC day when alert mail was held back by the budget. The count is what has been suppressed so far today."""
        day = utc_day(now)
        if self.ledger.get_meta("suppression_notified") == day:
            return False
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        count = self.ledger.read_one("SELECT COUNT(*) FROM messages WHERE state='budget_suppressed' AND kind IN ('alert','recovery','digest') AND updated_at>=?", (start,))[0]
        if not count:
            return False
        result = self._notice("suppression", f"suppression/{day}", {"reason": "budget", "count": count, "day": day}, now)
        if result == "failed":
            return False
        self.ledger.set_meta("suppression_notified", day)
        return result == "created"

    # -- the refresh result ---------------------------------------------------------------------------------------------------------
    def check_refresh_results(self, now: float) -> int:
        """One message per refresh attempt, from the records the refresh hooks leave in a read-only folder. The ledger key names the attempt and the outcome, so a repeat, a restart or two passes make
        one message. A record that is still waiting for the publisher is reported as unfinished after the configured wait, and a later final record is a second, different message.
        Returns how many messages were created."""
        r = self.config.refresh
        if not r.results_dir:
            return 0
        records, self.refresh_skipped = refresh_results.read_records(r.results_dir, r.max_files)
        made = 0
        for mtime, record in records:
            decided = refresh_results.plan(record, mtime, now, r.unfinished_minutes * 60)
            if decided is None:
                continue
            outcome, suffix = decided
            key = f"refresh/{record['key']}/{suffix}"
            if self.refresh_seen.get(key) == mtime:
                continue
            if self.ledger.by_key(key) is not None:
                self.refresh_seen[key] = mtime
                continue
            result = self._notice("refresh", key, refresh_results.context(record, outcome, self._local), now)
            if result != "failed":
                self.refresh_seen[key] = mtime
            made += result == "created"
        return made

    # -- the breaker --------------------------------------------------------------------------------------------------------------
    def note_breaker(self, event: str, now: float) -> None:
        """Remember when the breaker opened, so the digest can say how long mail was held."""
        if event == "opened":
            self.ledger.set_meta("breaker_opened_at", now)

    def digest_after_close(self, now: float) -> bool:
        """The breaker has closed: store one digest of the mail that was held, and end that mail in the same transaction.

        The duty is a flag the ledger sets when the breaker closes, so it survives a crash or a failed write and is picked up on a later pass. The digest's key is the episode (when the breaker opened),
        so a repeat can never make a second digest, and the held mail is ended only together with the digest's own commit. If nothing is held and no incidents were let go, the flag is simply cleared. If nothing is held but a dropped count remains, a count-only digest is stored, and it clears the flag and the count in the same commit."""
        due = self.ledger.get_meta("digest_due")
        if due is None or self.ledger.breaker().get("state") != "closed":
            return False
        held = self.ledger.held_for_digest()
        dropped = self.ledger.get_meta("digest_dropped", 0) or 0
        if not held and not dropped:
            self.ledger.set_meta("digest_due", None)
            return False                                                # nothing was held and nothing was let go: there is nothing to report
        opened = self.ledger.get_meta("breaker_opened_at")
        fired = {(h["context"].get("group_name") or "alert") for h in held if h["kind"] == "alert"}
        resolved = {(h["context"].get("group_name") or "alert") for h in held if h["kind"] == "recovery"}
        context = messages.digest_context(held, sorted(fired - resolved), self._local(opened) if opened else None, self._local(now), self.zone, dropped)         # with nothing held but some incidents let go, this is a count-only digest, and it is stored and clears the duty in one commit
        try:
            _, body = self.renderer.message("digest", context)
            result = self.ledger.persist(kind="digest", key=f"digest/{due['episode']}", body=body, now=now, deadline=now + NOTICE_HOURS * HOUR, replacing=[h["id"] for h in held],
                                         meta_set={"digest_due": None, "digest_dropped": 0})
        except (StorageRefused, StorageUnavailable):
            log.error("the digest could not be stored, so the held mail is left as it is and the next pass tries again")
            return False
        if not result.created and result.id:
            self.ledger.set_meta("digest_due", None)                    # this episode's digest is already stored
            return False
        if result.id == 0:
            return False                                                # no budget for it: the held mail stays and is sent as ordinary mail
        return True
