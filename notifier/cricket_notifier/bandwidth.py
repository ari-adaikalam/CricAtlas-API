"""The hourly bandwidth collector.

The monthly bandwidth figure (and the one in each summary) is kept by the whole UTC hour in the ledger, so a month can be summed whatever its boundary. Each hour is the reset-corrected change of the public
interface's transmitted-bytes counter (node exporter), read from Prometheus as raw samples with the machine's boot time beside it, as for any counter in accounting.py. Only completed hours are stored.
An hour Prometheus could not answer for stays unstored, which the summary counts as unmeasured, and is tried again on later passes for a few days. A stored hour is never changed.
"""
import logging

from cricket_notifier import accounting as acc
from cricket_notifier.config import Config
from cricket_notifier.ledger import Ledger
from cricket_notifier.prometheus import PrometheusUnavailable, TooManySamples

log = logging.getLogger("cricket_notifier.bandwidth")
HOUR = acc.HOUR
WINDOW_HOURS = 72           # how far back an unmeasured hour is still looked for, and how far the first pass fills in
RETRY_SECONDS = 600         # at most one failed read per ten minutes


class BandwidthCollector:
    def __init__(self, source, ledger: Ledger, config: Config, clock):
        self.source, self.ledger, self.config, self.clock = source, ledger, config, clock
        self.next_try = 0.0
        self.last_error: str | None = None

    def selectors(self) -> tuple[str, str]:
        p = self.config.prometheus                     # these names were checked to be plain when the configuration was loaded
        return f'node_network_transmit_bytes_total{{job="{p.node_job}",device="{p.bandwidth_device}"}}', f'node_boot_time_seconds{{job="{p.node_job}"}}'

    def collect(self, now: float) -> dict:
        complete = int(now) // HOUR                    # hours before this one have ended
        first = complete - WINDOW_HOURS
        stored = self.ledger.bandwidth_hours(first, complete)
        pending = [h for h in range(first, complete) if h not in stored]
        if not pending or now < self.next_try:
            return {"stored": 0, "pending": len(pending), "unavailable": False}
        p = self.config.prometheus
        earliest = pending[0] * HOUR - p.lookback_hours * 3600
        try:
            counter_selector, boot_selector = self.selectors()
            counter = self.source.matrix(counter_selector, now, now - earliest)
            boot = self.source.matrix(boot_selector, now, now - earliest)
        except (PrometheusUnavailable, TooManySamples) as error:
            self.next_try = now + RETRY_SECONDS
            self.last_error = type(error).__name__
            log.warning("bandwidth read failed: %s", self.last_error)
            return {"stored": 0, "pending": len(pending), "unavailable": True}
        self.last_error = None
        totals = acc.aggregate(counter).get((), [])
        boots = sorted(boot[0]["values"]) if boot else []
        samples = acc.attach_start(totals, boots)
        max_gap = p.scrape_interval_seconds * p.max_boundary_lag_intervals
        rows = []
        for hour in pending:
            size, measured = acc.bandwidth_hour(samples, hour, max_gap)
            if measured:
                rows.append((hour, size))
        self.ledger.store_bandwidth(rows)
        return {"stored": len(rows), "pending": len(pending) - len(rows), "unavailable": False}
