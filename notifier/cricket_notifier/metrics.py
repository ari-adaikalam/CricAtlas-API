"""The notifier's own metrics, in Prometheus text format, on a separate listener (GET /metrics only).

Every label value comes from a closed set (message kinds, states, breaker states, storage modes, webhook results), so the number of series is fixed whatever arrives at the webhook. Nothing taken from
a webhook, a recipient address, a key or a message body appears here. Prometheus alerts on these (a summary overdue, a message past its deadline, failed or unconfirmed messages, an open breaker,
the storage mode), which is how a broken email path is noticed when email itself is what broke.
"""
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from datetime import datetime, timezone

from cricket_notifier import accounting as acc
from cricket_notifier import budget as budget_module
from cricket_notifier.config import Config
from cricket_notifier.ledger import Ledger, utc_day, utc_month
from cricket_notifier.payload import KINDS
from cricket_notifier.states import STATES

log = logging.getLogger("cricket_notifier.metrics")
BREAKER_STATES = ("closed", "open", "half_open")
STORAGE_MODES = ("ok", "wal_over_target", "low_priority_refused", "full")
EVENTS = ("changed_payload", "expired_deadline", "expired_digest", "expired_protected", "expired_queue_overflow")
CLASSES = (budget_module.SUMMARY, budget_module.REGULAR, budget_module.NOTICE)


def _line(name: str, value, **labels) -> str:
    text = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return f"{name}{{{text}}} {value}" if text else f"{name} {value}"


class Metrics:
    def __init__(self, config: Config, ledger: Ledger, webhook=None, renderer=None, clock=time.time):
        self.config, self.ledger, self.webhook, self.renderer, self.clock = config, ledger, webhook, renderer, clock
        self.started = clock()
        self.last_tick: float | None = None          # set by the service loop: when it last completed a pass

    def render(self) -> str:
        now = self.clock()
        out: list = []

        def family(name: str, kind: str, help_text: str):
            out.append(f"# HELP {name} {help_text}")
            out.append(f"# TYPE {name} {kind}")

        counts = self.ledger.counts()
        family("cricket_notifier_messages", "gauge", "Messages in the ledger by kind and state.")
        for kind in KINDS:
            for state in STATES:
                out.append(_line("cricket_notifier_messages", counts.get((kind, state), 0), kind=kind, state=state))

        accepted = self.ledger.read_one("SELECT MAX(accepted_at) FROM messages WHERE state='accepted'")[0]
        family("cricket_notifier_last_accepted_timestamp_seconds", "gauge", "When Resend last accepted a message, 0 if none is in the ledger. For information: a quiet day has only the daily summary, so this is not a fault signal.")
        out.append(_line("cricket_notifier_last_accepted_timestamp_seconds", int(accepted or 0)))

        report = self.ledger.overdue_report(now, self.config.schedule.grace_minutes * 60)
        for name, key, text in (("cricket_notifier_summary_overdue_seconds", "summary_overdue_seconds", "How late a due summary is past its grace period, 0 when none is."),
                                ("cricket_notifier_oldest_overshoot_seconds", "oldest_overshoot_seconds", "How far the most overdue unsent message is past its deadline, 0 when none is."),
                                ("cricket_notifier_failed_or_unconfirmed", "failed_or_unconfirmed", "Messages that failed or are unconfirmed and not yet reviewed.")):
            family(name, "gauge", text)
            out.append(_line(name, report[key]))

        events = self.ledger.events()
        family("cricket_notifier_events_total", "counter", "Ledger events by name.")
        for event in EVENTS:
            out.append(_line("cricket_notifier_events_total", events.get(event, 0), event=event))

        breaker = self.ledger.breaker()
        family("cricket_notifier_breaker_state", "gauge", "1 for the current circuit breaker state.")
        for state in BREAKER_STATES:
            out.append(_line("cricket_notifier_breaker_state", int(breaker.get("state") == state), state=state))
        family("cricket_notifier_breaker_failures", "gauge", "Consecutive send failures counted by the breaker.")
        out.append(_line("cricket_notifier_breaker_failures", breaker.get("failures", 0)))

        usage, b = self.ledger.usage(now), self.config.budgets
        limits = {"day": budget_module.Limits.day(b), "month": budget_module.Limits.month(b)}
        family("cricket_notifier_budget_used", "gauge", "Messages the notifier has charged in the current UTC day or month, by class.")
        for period in ("day", "month"):
            for klass in CLASSES:
                out.append(_line("cricket_notifier_budget_used", usage[period].get(klass, 0), period=period, klass=klass))
        family("cricket_notifier_budget_limit", "gauge", "The most each class may charge in a UTC day or month.")
        for period in ("day", "month"):
            lim = limits[period]
            for klass, value in ((budget_module.SUMMARY, lim.summary), (budget_module.REGULAR, lim.regular), (budget_module.NOTICE, lim.pool)):
                out.append(_line("cricket_notifier_budget_limit", value, period=period, klass=klass))

        observed = self.ledger.get_meta("observed", {})
        family("cricket_notifier_account_used", "gauge", "Account usage Resend last reported for the current UTC day or month. Absent when none is known for it.")
        if observed.get("day") == utc_day(now) and "daily_used" in observed:
            out.append(_line("cricket_notifier_account_used", observed["daily_used"], period="day"))
        if observed.get("month") == utc_month(now) and "monthly_used" in observed:
            out.append(_line("cricket_notifier_account_used", observed["monthly_used"], period="month"))

        block = self.ledger.get_meta("quota_block_until", 0) or 0
        family("cricket_notifier_quota_blocked_until_timestamp_seconds", "gauge", "When a quota refusal from Resend stops blocking sends, 0 when not blocked.")
        out.append(_line("cricket_notifier_quota_blocked_until_timestamp_seconds", block if block > now else 0))

        month_start = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        first_hour, this_hour = int(month_start.timestamp()) // acc.HOUR, int(now) // acc.HOUR
        month = acc.sum_bandwidth([(r[0], r[1], r[2]) for r in self.ledger.read("SELECT hour, bytes, measured FROM bandwidth WHERE hour>=? AND hour<?", (first_hour, this_hour))], first_hour, this_hour)
        family("cricket_notifier_bandwidth_month_bytes", "gauge", "Bytes sent on the public interface in the current UTC month, summed over the whole hours that were measured. At least this much, see the unmeasured hours.")
        out.append(_line("cricket_notifier_bandwidth_month_bytes", month["bytes"]))
        family("cricket_notifier_bandwidth_month_unmeasured_hours", "gauge", "Whole hours of the current UTC month so far that were not measured.")
        out.append(_line("cricket_notifier_bandwidth_month_unmeasured_hours", month["unmeasured_hours"]))

        status = self.ledger.storage_status()
        family("cricket_notifier_storage_mode", "gauge", "1 for the current storage guard mode.")
        for mode in STORAGE_MODES:
            out.append(_line("cricket_notifier_storage_mode", int(status.mode == mode), mode=mode))
        for name, value, text in (("cricket_notifier_storage_used_bytes", status.used_bytes, "Bytes of the ledger in use."), ("cricket_notifier_storage_db_bytes", status.db_bytes, "Size of the ledger file."),
                                  ("cricket_notifier_storage_wal_bytes", status.wal_bytes, "Size of the write-ahead log."), ("cricket_notifier_storage_free_bytes", status.free_bytes, "Free space on the ledger's volume.")):
            family(name, "gauge", text)
            out.append(_line(name, value))

        if self.webhook is not None:
            family("cricket_notifier_webhook_requests_total", "counter", "Webhook requests by result.")
            for result, value in dict(self.webhook.counters).items():
                out.append(_line("cricket_notifier_webhook_requests_total", value, result=result))
        if self.renderer is not None:
            family("cricket_notifier_render_total", "counter", "Messages that needed the shortened or the minimal template.")
            for outcome, value in dict(self.renderer.counters).items():
                out.append(_line("cricket_notifier_render_total", value, outcome=outcome))

        family("cricket_notifier_process_start_time_seconds", "gauge", "When the notifier process started.")
        out.append(_line("cricket_notifier_process_start_time_seconds", int(self.started)))
        family("cricket_notifier_last_tick_timestamp_seconds", "gauge", "When the service loop last completed a pass, 0 before the first.")
        out.append(_line("cricket_notifier_last_tick_timestamp_seconds", int(self.last_tick or 0)))
        return "\n".join(out) + "\n"


class MetricsServer:
    def __init__(self, config: Config, metrics: Metrics, host: str | None = None, port: int | None = None):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 10

            def log_message(self, *args):
                pass

            def _send(self, status: int, body: bytes, ctype="text/plain; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_GET(self):
                if self.requestline.split(" ")[1:2] != ["/metrics"]:
                    return self._send(404, b"not found\n")
                try:
                    body = outer.metrics.render().encode()
                except Exception:
                    log.exception("metrics render failed")
                    return self._send(500, b"error\n")
                self._send(200, body, "text/plain; version=0.0.4; charset=utf-8")

            def _no(self):
                self._send(405, b"method not allowed\n")
            do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

            def do_HEAD(self):
                self._no()

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 16

        self.metrics = metrics
        self.server = Server((host or config.metrics.bind_host, config.metrics.bind_port if port is None else port), Handler)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), name="metrics", daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
