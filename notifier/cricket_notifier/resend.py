"""The Resend client: one POST, the stored bytes, the idempotency key, and a fixed set of outcomes.

What comes back is reduced to an outcome kind, a status code, an allow-listed error name and some numbers. The text of a response is never kept, logged or stored, because it could repeat what was sent.
The client never follows a redirect (the API key would go with it) and has no logger, so the key cannot reach a log from here. A timeout, a reset connection or a 5xx leaves the outcome unknown: the
message may or may not have been accepted, and only a repeat with the same key and bytes settles it.
"""
import http.client
import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

ACCEPTED, RATE_LIMITED, QUOTA_DAILY, QUOTA_MONTHLY = "accepted", "rate_limited", "quota_daily", "quota_monthly"
CONCURRENT, CHANGED_PAYLOAD, PERMANENT, SERVER_ERROR, NETWORK_ERROR = "concurrent", "changed_payload", "permanent", "server_error", "network_error"
KINDS = (ACCEPTED, RATE_LIMITED, QUOTA_DAILY, QUOTA_MONTHLY, CONCURRENT, CHANGED_PAYLOAD, PERMANENT, SERVER_ERROR, NETWORK_ERROR)
DEFINITIVE = (RATE_LIMITED, QUOTA_DAILY, QUOTA_MONTHLY, CHANGED_PAYLOAD, PERMANENT)      # Resend answered and did not accept the message
UNKNOWN = (CONCURRENT, SERVER_ERROR, NETWORK_ERROR)                                       # it may have been accepted
NAME = re.compile(r"[a-z][a-z_]{0,63}")
MAX_RESPONSE_BYTES = 8192


@dataclass(frozen=True)
class Outcome:
    kind: str
    status: int | None = None
    resend_id: str | None = None
    error_name: str | None = None
    retry_after: float | None = None
    daily_used: int | None = None
    monthly_used: int | None = None

    @property
    def definitive(self) -> bool:
        return self.kind in DEFINITIVE


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _int_header(headers, name: str, high: int) -> int | None:
    value = headers.get(name)
    if value is None or not re.fullmatch(r"[0-9]{1,9}", value.strip()):
        return None
    return min(int(value), high)


class ResendClient:
    def __init__(self, api_base: str, api_key: str, timeout: float = 10.0):
        self._url = api_base.rstrip("/") + "/emails"
        self._key = api_key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(NoRedirect())

    def __repr__(self) -> str:
        return f"ResendClient(url={self._url!r}, timeout={self.timeout})"

    def send(self, body: bytes, idempotency_key: str) -> Outcome:
        request = urllib.request.Request(self._url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Idempotency-Key": idempotency_key, "User-Agent": "cricket-notifier/0.1", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return self._classify(response.status, response.headers, response.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as error:
            try:
                raw = error.read(MAX_RESPONSE_BYTES)
            except (OSError, http.client.HTTPException):
                raw = b""
            return self._classify(error.code, error.headers, raw)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, http.client.HTTPException, OSError, ValueError):
            return Outcome(NETWORK_ERROR)

    @staticmethod
    def _classify(status: int, headers, raw: bytes) -> Outcome:
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        data = data if isinstance(data, dict) else {}
        name = data.get("name")
        name = name if isinstance(name, str) and NAME.fullmatch(name) else None
        daily, monthly = _int_header(headers, "x-resend-daily-quota", 10 ** 9), _int_header(headers, "x-resend-monthly-quota", 10 ** 9)
        common = dict(status=status, daily_used=daily, monthly_used=monthly)
        if 200 <= status < 300:
            resend_id = data.get("id")
            if isinstance(resend_id, str) and 1 <= len(resend_id) <= 100 and re.fullmatch(r"[A-Za-z0-9._-]+", resend_id):
                return Outcome(ACCEPTED, resend_id=resend_id, **common)
            return Outcome(SERVER_ERROR, error_name="no_message_id", **common)      # a success without an id cannot be recorded as accepted, and may have been
        if status == 429:
            if name == "daily_quota_exceeded":
                return Outcome(QUOTA_DAILY, error_name=name, **common)
            if name == "monthly_quota_exceeded":
                return Outcome(QUOTA_MONTHLY, error_name=name, **common)
            return Outcome(RATE_LIMITED, error_name=name or "rate_limit_exceeded", retry_after=_int_header(headers, "retry-after", 86_400), **common)
        if status == 409:
            if name == "invalid_idempotent_request":
                return Outcome(CHANGED_PAYLOAD, error_name=name, **common)
            return Outcome(CONCURRENT, error_name=name or "concurrent_idempotent_requests", **common)
        if 300 <= status < 400:                            # a redirect means the address is wrong, and it will not be followed, so it cannot succeed on a retry
            return Outcome(PERMANENT, error_name="redirect", **common)
        if 400 <= status < 500:
            return Outcome(PERMANENT, error_name=name or "other", **common)
        return Outcome(SERVER_ERROR, error_name=name or "other", **common)
