"""Sending: begin the attempt in the ledger, send the stored bytes, record the outcome.

The order is the whole point. The ledger records that an attempt is starting, and commits, before a byte goes to Resend. So a crash leaves an attempt whose outcome is unknown (the row says `sending`),
never a send that nobody recorded, and a restart settles it by repeating the same bytes with the same key, which Resend cannot accept twice inside its window. If the ledger cannot record the
beginning or the end of an attempt, nothing more is sent: sending without a record is the one way to make a duplicate that nothing tracks.
"""
import logging
import time
from dataclasses import dataclass, field

from cricket_notifier import payload
from cricket_notifier.errors import IllegalTransition, StorageUnavailable
from cricket_notifier.ledger import Attempt, Blocked, Ledger
from cricket_notifier.resend import PERMANENT, Outcome, ResendClient

log = logging.getLogger("cricket_notifier.sender")
GLOBAL_BLOCKS = ("quota_block", "account_headroom_daily", "account_headroom_monthly")      # these stop every message, so the loop stops


@dataclass
class RunReport:
    attempted: int = 0
    accepted: int = 0
    blocked: dict = field(default_factory=dict)
    breaker_events: list = field(default_factory=list)
    storage_unavailable: bool = False


class Sender:
    def __init__(self, ledger: Ledger, client: ResendClient, clock=time.time, hook=None):
        self.ledger, self.client, self.clock = ledger, client, clock
        self.hook = hook                                   # hook(stage, message_id, detail): a seam for the crash tests, never set in production
        self.attempts_total = 0
        self.last_accepted_at: float | None = None

    def _hook(self, stage: str, msg_id: int, detail=None) -> None:
        if self.hook is not None:
            self.hook(stage, msg_id, detail)

    def send_one(self, row, now: float, probe: bool = False) -> tuple[Attempt | Blocked, Outcome | None, object]:
        """One attempt of one message. Returns what began, the outcome, and what the ledger said about it."""
        begun = self.ledger.begin_attempt(row["id"], now, probe=probe)
        if isinstance(begun, Blocked):
            return begun, None, None
        self._hook("after_begin", row["id"], begun)
        if payload.sha256(begun.body) != begun.sha256:       # the stored bytes are not the bytes that were committed: never send them
            outcome = Outcome(PERMANENT, error_name="payload_hash_mismatch")
        else:
            outcome = self.client.send(begun.body, begun.idempotency_key)
        self.attempts_total += 1
        self._hook("after_response", row["id"], outcome)
        finished = self.ledger.finish_attempt(row["id"], outcome, self.clock())
        if outcome.kind == "accepted":
            self.last_accepted_at = self.clock()
        return begun, outcome, finished

    def run_due(self, now: float | None = None, limit: int = 50) -> RunReport:
        """Send what is due, in priority order. Stops when the ledger cannot record, when a global block applies, or after a failed probe."""
        report = RunReport()
        now = self.clock() if now is None else now
        for row in self.ledger.due(now, limit):
            gate = self.ledger.breaker_gate(row["kind"], now)
            if gate == "hold":
                report.blocked["breaker"] = report.blocked.get("breaker", 0) + 1
                continue
            try:
                begun, outcome, finished = self.send_one(row, now, probe=(gate == "probe"))
            except StorageUnavailable:
                report.storage_unavailable = True
                log.error("the ledger cannot record an attempt, so nothing more is sent")
                return report
            except IllegalTransition:                         # the row changed under us (another path settled it): skip it
                continue
            if isinstance(begun, Blocked):
                report.blocked[begun.reason] = report.blocked.get(begun.reason, 0) + 1
                if begun.reason in GLOBAL_BLOCKS:
                    return report
                continue
            report.attempted += 1
            report.accepted += outcome.kind == "accepted"
            if finished.breaker_event:
                report.breaker_events.append(finished.breaker_event)
            if gate == "probe" and outcome.kind != "accepted":
                return report                                 # one probe at a time: a failed one ends the run
            if finished.breaker_event == "closed":
                return report                                 # the breaker has just closed: what was held is replaced by one digest before anything else goes, so the run ends here
        return report
