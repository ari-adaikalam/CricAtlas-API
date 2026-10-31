"""A stand-in for Resend, as a real local HTTP server: the same shapes of answer, the same idempotency rule, and a record of every email it actually accepted.

It is told what to do with each request by a script (a list of behaviours used in order, then 'accept'), so a test can make the notifier see a timeout when the email was in fact accepted, which is the
case that matters. No test talks to the real service.
"""
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WINDOW = 24 * 3600


class Standin:
    def __init__(self):
        self.script: list = []
        self.emails: list[dict] = []                 # every email accepted: {id, key, sha256, count}
        self.requests: list[dict] = []               # every request seen: {key, sha256, behaviour, auth}
        self.keys: dict[str, tuple] = {}             # idempotency key -> (sha256, status, body, at)
        self.now = time.time()
        self.lock = threading.Lock()
        self.daily_quota, self.monthly_quota = 0, 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                outer._handle(self, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _reply(self, handler, status, data, headers=None):
        raw = json.dumps(data).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            handler.send_header(name, str(value))
        handler.end_headers()
        handler.wfile.write(raw)

    def _handle(self, handler, body):
        key = handler.headers.get("Idempotency-Key", "")
        sha = hashlib.sha256(body).hexdigest()
        with self.lock:
            behaviour = self.script.pop(0) if self.script else "accept"
            kind = behaviour if isinstance(behaviour, str) else behaviour["kind"]
            self.requests.append({"key": key, "sha256": sha, "behaviour": kind, "auth": handler.headers.get("Authorization", ""), "bytes": len(body)})
            quota = {"x-resend-daily-quota": self.daily_quota, "x-resend-monthly-quota": self.monthly_quota}
            known = self.keys.get(key)
            if known and self.now - known[3] >= WINDOW:
                known = None                              # the key has expired, as it does after 24 hours
            if kind in ("accept", "accept_then_timeout", "accept_then_reset"):
                if known:
                    if known[0] != sha:
                        return self._reply(handler, 409, {"statusCode": 409, "name": "invalid_idempotent_request", "message": "zzSTANDINTEXTzz"})
                    status, data = known[1], known[2]
                else:
                    self.daily_quota += 1
                    self.monthly_quota += 1
                    data = {"id": f"em_{len(self.emails) + 1:06d}"}
                    status = 200
                    self.emails.append({"id": data["id"], "key": key, "sha256": sha, "count": 1})
                    self.keys[key] = (sha, status, data, self.now)
                quota = {"x-resend-daily-quota": self.daily_quota, "x-resend-monthly-quota": self.monthly_quota}
        if kind == "accept":
            return self._reply(handler, status, data, quota)
        if kind == "accept_then_timeout":
            time.sleep(float(behaviour.get("hold", 1.5)) if isinstance(behaviour, dict) else 1.5)
            try:
                return self._reply(handler, status, data, quota)
            except OSError:
                return
        if kind == "accept_then_reset":
            handler.connection.close()
            return
        if kind == "drop":                                # nothing accepted, connection closed
            handler.connection.close()
            return
        if kind == "timeout":                             # nothing accepted, no answer in time
            time.sleep(1.5)
            try:
                return self._reply(handler, 200, {"id": "em_never"})
            except OSError:
                return
        if kind == "500":
            return self._reply(handler, 500, {"statusCode": 500, "name": "internal_server_error", "message": "zzSTANDINTEXTzz"}, quota)
        if kind == "503":
            return self._reply(handler, 503, {"statusCode": 503, "name": "application_error", "message": "zzSTANDINTEXTzz"})
        if kind == "429":
            return self._reply(handler, 429, {"statusCode": 429, "name": "rate_limit_exceeded", "message": "zzSTANDINTEXTzz"}, {"Retry-After": behaviour.get("retry_after", 2) if isinstance(behaviour, dict) else 2})
        if kind == "429_daily":
            return self._reply(handler, 429, {"statusCode": 429, "name": "daily_quota_exceeded", "message": "zzSTANDINTEXTzz"})
        if kind == "429_monthly":
            return self._reply(handler, 429, {"statusCode": 429, "name": "monthly_quota_exceeded", "message": "zzSTANDINTEXTzz"})
        if kind == "409_concurrent":
            return self._reply(handler, 409, {"statusCode": 409, "name": "concurrent_idempotent_requests", "message": "zzSTANDINTEXTzz"})
        if kind == "409_changed":
            return self._reply(handler, 409, {"statusCode": 409, "name": "invalid_idempotent_request", "message": "zzSTANDINTEXTzz"})
        if kind in ("400", "401", "403", "422"):
            names = {"400": "invalid_parameter", "401": "missing_api_key", "403": "invalid_from_address", "422": "validation_error"}
            return self._reply(handler, int(kind), {"statusCode": int(kind), "name": names[kind], "message": "zzSTANDINTEXTzz"})
        if kind == "redirect":
            handler.send_response(307)
            handler.send_header("Location", "http://127.0.0.1:1/elsewhere")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        if kind == "no_id":
            return self._reply(handler, 200, {"ok": True})
        if kind == "html":
            raw = b"<html>nope</html>"
            handler.send_response(502)
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
            return
        raise AssertionError(f"unknown behaviour {kind}")

    @property
    def sent(self):
        return len(self.emails)
