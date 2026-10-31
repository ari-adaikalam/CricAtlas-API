"""The publisher's outgoing HTTP requests: the public checks and the Cloudflare purge.

Cloudflare's edge on this zone answers 403 (error 1010) to urllib's default User-Agent, so every request the publisher makes names itself. These tests hold that, and hold the rest of
the request code as it was: TLS verification by the default handlers, no redirects, a 30 second timeout, the caller's headers (the authorisation header) kept, the hostname-only purge payload.
No network is used: a local server and a stand-in opener capture what would be sent.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from cricket_refresh import activation as a

HOSTNAME = "api.example.org"
ZONE = "0123456789abcdef0123456789abcdef"
TOKEN = "T" * 40


class Capture(BaseHTTPRequestHandler):
    seen = []
    status, location = 200, None

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        Capture.seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": self.rfile.read(length)})
        self.send_response(Capture.status)
        if Capture.location:
            self.send_header("Location", Capture.location)
        body = b'{"ok": true}'
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    Capture.seen, Capture.status, Capture.location = [], 200, None
    httpd = HTTPServer(("127.0.0.1", 0), Capture)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def header(seen, name):
    return {k.lower(): v for k, v in seen["headers"].items()}.get(name.lower())


def test_a_get_names_itself_and_keeps_the_json_content_type(server):
    body, headers = a.request_json(server + "/status")
    seen = Capture.seen[0]
    assert body == {"ok": True} and seen["method"] == "GET" and seen["body"] == b""
    assert header(seen, "User-Agent") == "CricAtlasRefresh/1.0"
    assert header(seen, "Content-Type") == "application/json"


def test_a_post_keeps_its_payload_authorisation_and_names_itself(server):
    a.request_json(server + "/purge", data={"hosts": [HOSTNAME]}, headers={"Authorization": f"Bearer {TOKEN}"})
    seen = Capture.seen[0]
    assert seen["method"] == "POST" and json.loads(seen["body"]) == {"hosts": [HOSTNAME]}
    assert header(seen, "Authorization") == f"Bearer {TOKEN}"
    assert header(seen, "User-Agent") == "CricAtlasRefresh/1.0" and header(seen, "Content-Type") == "application/json"


def test_a_header_the_caller_passes_still_wins(server):
    a.request_json(server + "/x", headers={"User-Agent": "other/2"})
    assert header(Capture.seen[0], "User-Agent") == "other/2"


def test_a_redirect_is_still_refused(server):
    Capture.status, Capture.location = 302, "http://127.0.0.1:1/elsewhere"
    with pytest.raises(ValueError, match="redirect"):
        a.request_json(server + "/status")
    assert len(Capture.seen) == 1, "the redirect target was never requested"


def test_an_error_status_still_raises_http_error_and_other_2xx_is_refused(server):
    Capture.status = 403
    with pytest.raises(HTTPError):
        a.request_json(server + "/status")
    Capture.status = 201
    with pytest.raises(ValueError, match="unexpected HTTP response"):
        a.request_json(server + "/status")


class FakeResponse:
    def __init__(self, body, headers=None, status=200):
        self._body, self.status, self.headers = json.dumps(body).encode(), status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=None):
        return self._body


class FakeOpener:
    """Stands in for urllib's opener: records the handlers it was built with, the request and the timeout, and answers from a table."""
    built, requests = [], []
    answers = {}

    def __init__(self, *handlers):
        FakeOpener.built.append(handlers)

    def open(self, request, timeout=None):
        FakeOpener.requests.append((request, timeout))
        return FakeOpener.answers[request.full_url]


@pytest.fixture()
def opener(monkeypatch):
    FakeOpener.built, FakeOpener.requests, FakeOpener.answers = [], [], {}
    monkeypatch.setattr(a, "build_opener", FakeOpener)
    return FakeOpener


def test_tls_default_handlers_redirect_refusal_and_the_timeout_are_unchanged(opener):
    opener.answers["https://example.invalid/x"] = FakeResponse({})
    a.request_json("https://example.invalid/x")
    assert opener.built == [(a.NoRedirect,)], "only the redirect refusal is added, so urllib's default HTTPS handler and certificate validation stay in force"
    assert opener.requests[0][1] == 30


def host(tmp_path):
    h = object.__new__(a.Host)
    h.root, h.policy = tmp_path, {"hostname": HOSTNAME}
    h.credentials = lambda: {"zone_id": ZONE, "api_token": TOKEN}
    return h


def test_the_purge_is_hostname_only_authenticated_and_names_itself(tmp_path, opener):
    opener.answers[f"https://api.cloudflare.com/client/v4/zones/{ZONE}/purge_cache"] = FakeResponse({"success": True})
    host(tmp_path).purge()
    request, timeout = opener.requests[0]
    assert request.get_method() == "POST" and request.full_url == f"https://api.cloudflare.com/client/v4/zones/{ZONE}/purge_cache"
    assert json.loads(request.data) == {"hosts": [HOSTNAME]}, "never purge_everything, prefixes, tags or another hostname"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("User-agent") == "CricAtlasRefresh/1.0" and request.get_header("Content-type") == "application/json"
    assert timeout == 30


def test_a_purge_the_service_does_not_confirm_is_a_failure(tmp_path, opener):
    opener.answers[f"https://api.cloudflare.com/client/v4/zones/{ZONE}/purge_cache"] = FakeResponse({"success": False})
    with pytest.raises(ValueError, match="did not confirm"):
        host(tmp_path).purge()


def test_both_public_checks_are_get_requests_that_name_themselves(tmp_path, opener):
    release = tmp_path / "releases" / "data-20261028T122636Z-2a3579eb5b8f-c3b863aa"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text(json.dumps({"data": {"version": "2a3579eb5b8f"}, "golden": {"/v1/meta": {"etag": 'W/"abc"'}}}))
    opener.answers[f"https://{HOSTNAME}/status"] = FakeResponse({"data_version": "2a3579eb5b8f"})
    opener.answers[f"https://{HOSTNAME}/v1/meta"] = FakeResponse({"data": {"data_version": "2a3579eb5b8f"}}, headers={"ETag": 'W/"abc"'})
    host(tmp_path).public_check("data-20261028T122636Z-2a3579eb5b8f-c3b863aa")
    assert [r.full_url for r, _ in opener.requests] == [f"https://{HOSTNAME}/status", f"https://{HOSTNAME}/v1/meta"]
    for request, timeout in opener.requests:
        assert request.get_method() == "GET" and request.data is None and timeout == 30
        assert request.get_header("User-agent") == "CricAtlasRefresh/1.0"
        assert request.get_header("Authorization") is None, "the public checks carry no credential"


def test_the_public_check_still_refuses_another_snapshot(tmp_path, opener):
    release = tmp_path / "releases" / "data-20261028T122636Z-2a3579eb5b8f-c3b863aa"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text(json.dumps({"data": {"version": "2a3579eb5b8f"}, "golden": {"/v1/meta": {"etag": 'W/"abc"'}}}))
    opener.answers[f"https://{HOSTNAME}/status"] = FakeResponse({"data_version": "5fc42bc763cd"})
    with pytest.raises(ValueError, match="another snapshot"):
        host(tmp_path).public_check("data-20261028T122636Z-2a3579eb5b8f-c3b863aa")


def test_the_public_check_uses_the_public_address_when_the_policy_names_one_and_the_purge_still_names_the_hostname(tmp_path, opener):
    release = tmp_path / "releases" / "data-20261028T122636Z-2a3579eb5b8f-c3b863aa"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text(json.dumps({"data": {"version": "2a3579eb5b8f"}, "golden": {"/v1/meta": {"etag": 'W/"abc"'}}}))
    base = "https://app.example.test/api"
    opener.answers[f"{base}/status"] = FakeResponse({"data_version": "2a3579eb5b8f"})
    opener.answers[f"{base}/v1/meta"] = FakeResponse({"data": {"data_version": "2a3579eb5b8f"}}, headers={"ETag": 'W/"abc"'})
    h = host(tmp_path)
    h.policy = {"hostname": HOSTNAME, "public_base": base}
    h.public_check("data-20261028T122636Z-2a3579eb5b8f-c3b863aa")
    assert [r.full_url for r, _ in opener.requests] == [f"{base}/status", f"{base}/v1/meta"], "no request goes to the retired hostname"
    opener.answers[f"https://api.cloudflare.com/client/v4/zones/{ZONE}/purge_cache"] = FakeResponse({"success": True})
    h.purge()
    assert json.loads(opener.requests[-1][0].data) == {"hosts": [HOSTNAME]}, "the origin's cached copies are still what is purged"


@pytest.mark.parametrize("bad", ["http://app.example.test/api", "https://app.example.test/api/", "https://app.example.test/a/b", "app.example.test/api", "https://", "https://app.example.test:8443/api", "https://App.example.test", 5, ""])
def test_a_malformed_public_address_in_the_policy_is_refused(bad):
    with pytest.raises(ValueError, match="invalid public API address"):
        a.check_public_base(bad)


@pytest.mark.parametrize("good", [None, "https://app.example.test", "https://app.example.test/api"])
def test_an_absent_or_well_formed_public_address_is_accepted(good):
    a.check_public_base(good)
