"""The Alertmanager webhook receiver.

One route, POST /alertmanager, on an address that only Alertmanager can reach (the launcher refuses a wildcard). Everything is checked before anything is stored, and in this order so that the cheap
refusals come first and an unauthenticated caller costs almost nothing:

  the path and the method, a rate limit, a limit on handlers at once, the bearer token (constant time, before the body is read), the content type, the length (required, at most 1 MB),
  the body read to exactly that length, JSON, then the shape Alertmanager sends (version "4", a status, 1 to 100 alerts).

A message is built from an allow list of fields, cut and cleaned (see messages.py), stored under a key that makes a repeated webhook one message, and only then is the 200 sent. If the ledger cannot take it
(storage short, or unwritable) the answer is 503, which Alertmanager retries. No answer repeats anything that was sent.
"""
import hmac
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cricket_notifier import messages
from cricket_notifier.config import Config
from cricket_notifier.errors import PayloadTooLarge, StorageUnavailable
from cricket_notifier.ledger import Ledger, StorageRefused
from cricket_notifier.render import Renderer

log = logging.getLogger("cricket_notifier.webhook")
RESULTS = ("ok", "duplicate", "suppressed", "unauthorized", "bad_request", "too_large", "rate_limited", "unavailable", "not_found", "method_not_allowed", "error")
MAX_HANDLERS = 16
SOCKET_TIMEOUT = 10


class WebhookServer:
    def __init__(self, config: Config, ledger: Ledger, renderer: Renderer, token: str, clock=time.time, host: str | None = None, port: int | None = None):
        self.config, self.ledger, self.renderer, self.clock = config, ledger, renderer, clock
        self._token = token.encode("utf-8")
        self.counters = {name: 0 for name in RESULTS}
        self._lock = threading.Lock()
        self._times: deque = deque()
        self._slots = threading.BoundedSemaphore(MAX_HANDLERS)
        self.on_stored = None                                  # called after a new message is committed, so the service can send it without waiting for its next pass
        self.zone = config.schedule.zone
        outer = self

        def target(h) -> str:
            """The path exactly as sent: http.server rewrites a leading // to /, which would let //alertmanager reach the route."""
            parts = h.requestline.split(" ")
            return parts[1] if len(parts) >= 2 else ""

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = SOCKET_TIMEOUT

            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.handle(self)

            def _other(self):
                outer.refuse(self, 404 if target(self) != "/alertmanager" else 405, "not_found" if target(self) != "/alertmanager" else "method_not_allowed")
            do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _other

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64                                   # read when the socket starts listening, so it must be set on the class

        self.server = Server((host or config.webhook.bind_host, config.webhook.bind_port if port is None else port), Handler)
        self._target = target
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), name="webhook", daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _count(self, result: str) -> None:
        with self._lock:
            self.counters[result] += 1

    def refuse(self, handler, status: int, result: str, headers: dict | None = None) -> None:
        """A refusal with a fixed small body. The connection is closed, because the body (if any) was not read."""
        self._count(result)
        body = json.dumps({"status": result}).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            handler.send_header(name, str(value))
        handler.end_headers()
        handler.wfile.write(body)
        handler.close_connection = True

    def _reply(self, handler, result: str) -> None:
        self._count(result)
        body = json.dumps({"status": result}).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _rate_ok(self, now: float) -> bool:
        with self._lock:
            while self._times and now - self._times[0] >= 60:
                self._times.popleft()
            if len(self._times) >= self.config.webhook.requests_per_minute:
                return False
            self._times.append(now)
            return True

    def handle(self, h) -> None:
        w = self.config.webhook
        now = self.clock()
        if self._target(h) != "/alertmanager":
            return self.refuse(h, 404, "not_found")
        if not self._rate_ok(now):
            return self.refuse(h, 429, "rate_limited", {"Retry-After": 60})
        if not self._slots.acquire(blocking=False):
            return self.refuse(h, 503, "unavailable", {"Retry-After": 10})
        try:
            auth = h.headers.get("Authorization", "")
            given = auth[7:].encode("utf-8", "replace") if auth.startswith("Bearer ") else b""
            if not hmac.compare_digest(given, self._token):                                    # constant time, and before a byte of the body is read
                return self.refuse(h, 401, "unauthorized", {"WWW-Authenticate": "Bearer"})
            if h.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                return self.refuse(h, 400, "bad_request")
            if "Transfer-Encoding" in h.headers:
                return self.refuse(h, 400, "bad_request")
            length = h.headers.get("Content-Length")
            if length is None:
                return self.refuse(h, 411, "bad_request")
            if not length.isascii() or not length.isdigit() or len(length) > 10:
                return self.refuse(h, 400, "bad_request")
            if int(length) > w.max_body_kb * 1024:
                return self.refuse(h, 413, "too_large")
            raw = h.rfile.read(int(length))
            if len(raw) != int(length):
                return self.refuse(h, 400, "bad_request")
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                return self.refuse(h, 400, "bad_request")
            problem = self.check(data)
            if problem == "too_many":
                return self.refuse(h, 413, "too_large")
            if problem:
                return self.refuse(h, 400, "bad_request")
            try:
                result = self.store(data, now)
            except (StorageRefused, StorageUnavailable):
                return self.refuse(h, 503, "unavailable", {"Retry-After": 30})
            except PayloadTooLarge:                                                             # the renderer keeps messages inside their limit, so this is a fault
                log.error("a webhook message was over its limit")
                return self.refuse(h, 500, "error")
            return self._reply(h, result)
        except Exception:                                                                       # nothing about the request is logged: it is data from outside
            log.error("a webhook request failed")
            return self.refuse(h, 500, "error")
        finally:
            self._slots.release()

    def check(self, data) -> str | None:
        """None when the body has the shape Alertmanager sends, else 'too_many' or 'invalid'."""
        if not isinstance(data, dict) or data.get("version") != "4" or data.get("status") not in ("firing", "resolved"):
            return "invalid"
        alerts = data.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            return "invalid"
        if len(alerts) > self.config.webhook.max_alerts:
            return "too_many"
        for alert in alerts:
            if not isinstance(alert, dict):
                return "invalid"
            for part in ("labels", "annotations"):
                if part in alert and not isinstance(alert[part], dict):
                    return "invalid"
        if "commonLabels" in data and not isinstance(data["commonLabels"], dict):
            return "invalid"
        return None

    def store(self, data: dict, now: float) -> str:
        """Build the message, store it, and say what happened: 'ok', 'duplicate' or 'suppressed' (stored, but the day's budget has no room for it)."""
        bucket = int(now // (self.config.deadlines.reminder_hours * 3600))
        key = messages.alert_key(data, bucket)
        kind = "recovery" if data["status"] == "resolved" else "alert"
        context = messages.alert_context(data, self.zone)
        _, body = self.renderer.message(kind, context)
        result = self.ledger.persist(kind=kind, key=key, body=body, now=now, context=messages.alert_stored_context(context), deadline=now + self.config.deadlines.alert_minutes * 60, low_priority=True)
        if not result.created:
            return "duplicate"
        if result.state == "budget_suppressed":
            return "suppressed"
        if self.on_stored is not None:
            self.on_stored()
        return "ok"
