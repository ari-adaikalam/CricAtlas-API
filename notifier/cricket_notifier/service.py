"""The service: one pass (`tick`) that does everything in order, and the loop that repeats it.

A pass: read the hourly bandwidth, create the summaries that have come due, send what is due, run the watchers (disk, the email path, the suppression notice), send what they stored, turn a closed breaker into
one digest, and keep the ledger tidy. Each step fails alone: one that raises is logged (by class, never with message text) and the next still runs. The state that matters lives in the ledger, so a pass can be
repeated or interrupted at any point.

Summaries are created at their slot, with the slot as the key, so a repeated pass, a restart in the middle of one, or two processes by mistake make one message. If the report cannot be built, a
plainer report that says so is sent in its place, so a day is never silently missing. A slot older than the catch-up window is recorded as skipped and not sent.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from cricket_notifier import payload, schedule
from cricket_notifier.errors import PayloadTooLarge, StorageUnavailable
from cricket_notifier.ledger import StorageRefused

log = logging.getLogger("cricket_notifier.service")
UTC = timezone.utc
TICK_SECONDS = 15
MAINTAIN_SECONDS = 600
MAX_SEND_PASSES = 3


class Service:
    def __init__(self, config, ledger, renderer, builder, sender, watcher, bandwidth, metrics=None, clock=time.time, tick_seconds: float = TICK_SECONDS):
        self.config, self.ledger, self.renderer, self.builder, self.sender, self.watcher, self.bandwidth, self.metrics = config, ledger, renderer, builder, sender, watcher, bandwidth, metrics
        self.clock, self.tick_seconds = clock, tick_seconds
        self.schedule = schedule.Schedule(config.schedule.zone, config.schedule.clock)
        self.wake = threading.Event()                                   # set when something new is stored, so it need not wait for the next pass
        self.last_maintained = 0.0
        self.step_failures: dict = {}
        self.started_report: dict = {}

    # -- start -----------------------------------------------------------------------------------------------------------------
    def start(self) -> dict:
        """What a restart does first: settle the messages left in `sending`, and note when monitoring first ran (the first report's period starts then)."""
        now = self.clock()
        self.started_report = self.ledger.recover_after_restart(now)
        if self.ledger.get_meta("first_started") is None:
            self.ledger.set_meta("first_started", now)
        return self.started_report

    # -- one pass ------------------------------------------------------------------------------------------------------------------
    def _step(self, name: str, function, *args):
        try:
            return function(*args)
        except StorageUnavailable:
            self.step_failures[name] = self.step_failures.get(name, 0) + 1
            log.error("step %s: the ledger cannot be written", name)
        except Exception as error:                                      # a step must never stop the next one, and never print what was in it
            self.step_failures[name] = self.step_failures.get(name, 0) + 1
            log.error("step %s failed: %s", name, type(error).__name__)
        return None

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self._step("bandwidth", self.bandwidth.collect, now)
        self._step("summaries", self.create_summaries, now)
        self._step("expire", self._expire, now)
        self._step("send", self.send, now)                              # before the watchers: a summary created just now (after a long stop, say) must get its try before it can look overdue
        self._step("disk", self.watcher.check_disk, now)
        self._step("email_path", self.watcher.check_email_path, now)
        self._step("suppression", self.watcher.flush_suppression, now)
        self._step("refresh_results", self.watcher.check_refresh_results, now)
        self._step("send_notices", self.send, now)                      # the notices the watchers just stored
        if now - self.last_maintained >= MAINTAIN_SECONDS:
            if self._step("maintain", self.ledger.maintain, now) is not None:
                self.last_maintained = now
        if self.metrics is not None:
            self.metrics.last_tick = now

    def _expire(self, now: float) -> None:
        self.ledger.expire_overdue(now)
        self.ledger.settle_windows(now)

    # -- summaries ---------------------------------------------------------------------------------------------------------------
    def create_summaries(self, now: float) -> int:
        first = self.ledger.get_meta("first_started")
        if first is None:
            return 0
        last = self.ledger.get_meta("last_boundary")
        plans = schedule.plan(self.schedule, datetime.fromtimestamp(now, UTC), datetime.fromtimestamp(last, UTC) if last is not None else None, datetime.fromtimestamp(first, UTC),
                              timedelta(hours=self.config.schedule.catch_up_hours))
        created = 0
        for p in plans:
            try:
                made = self._persist_summary(p, now)
            except (StorageRefused, StorageUnavailable):
                log.error("the summary for %s could not be stored", p.key)
                return created                                          # the boundary stays where it is, so the next pass tries this slot again
            created += made
            self.ledger.set_meta("last_boundary", p.slot.timestamp())
        return created

    def _persist_summary(self, p, now: float) -> int:
        slot_ts = p.slot.timestamp()
        common = dict(kind="summary", key=p.key, now=now, due_at=slot_ts, slot=p.slot.strftime("%Y-%m-%dT%H:%M:%SZ"), deadline=slot_ts + self.config.deadlines.summary_hours * 3600)
        if self.ledger.by_key(p.key) is not None:
            return 0                                                    # stored before a restart: nothing is built or counted twice
        if p.skip:
            return int(self.ledger.persist(body=None, skip=True, **common).created)
        body, context = self.build_summary(p, now)
        return int(self.ledger.persist(body=body, context=context, **common).created)

    def build_summary(self, p, now: float) -> tuple[bytes, dict]:
        try:
            context = self.builder.build(p, now)
            _, body = self.renderer.message("summary", context)
            return body, {"incomplete": bool(context.get("incomplete"))}
        except Exception as error:
            reason = type(error).__name__
            log.error("the summary for %s could not be built: %s", p.key, reason)
        try:
            context = self.builder.fallback_context(p, now, "an internal error")
            _, body = self.renderer.message("summary", context)
            return body, {"incomplete": True, "fallback": True}
        except Exception as error:
            log.error("the plain summary for %s could not be built either: %s", p.key, type(error).__name__)
        rendered = self.renderer.render("minimal", {"original_kind": "summary", "title": "Daily summary could not be built", "shrunk": False})
        e = self.config.email
        return payload.build(e.sender, e.recipient, rendered.subject, rendered.html, rendered.text), {"incomplete": True, "fallback": True}

    # -- sending ---------------------------------------------------------------------------------------------------------------------
    def send(self, now: float):
        """Send what is due. A breaker that opens is noted. When one has closed, the digest of what it held is stored first (the duty is a flag in the ledger, so it also survives a restart) and goes out
        in the next round of this loop."""
        report = None
        for _ in range(MAX_SEND_PASSES):
            self.watcher.digest_after_close(now)
            report = self.sender.run_due(now)
            closed = False
            for event in report.breaker_events:
                if event == "opened" or event == "reopened":
                    self.watcher.note_breaker("opened", now)
                elif event == "closed":
                    closed = True
            if not closed:
                break
        return report

    # -- the loop ----------------------------------------------------------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        self.start()
        while not stop.is_set():
            self.tick()
            self.wake.wait(self.tick_seconds)
            self.wake.clear()
