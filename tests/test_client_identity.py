"""Who a request comes from, and what the rate limit is kept for. The rules are in cricket_api/clients.py.

The cases that matter: a forwarding header is never believed unless the connection came from a proxy the operator listed, a valid looking address from anyone else is still ignored, the
chain is read from the right so a client cannot write its own address, a header that is not clean is refused, and a unix socket works only when it is set up for it. The last group runs a
real uvicorn on a unix socket and on TCP, because the test client cannot show what a real transport hands to the app. They start the server with the launcher, as the README tells.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cricket_api import clients
from cricket_api.app import create_app
from cricket_api.settings import Settings

GOLDEN = Path(__file__).parent / "golden" / "api"
BURST = 3


def build(dataset, peer=("203.0.113.9", 5000), unix=False, **settings):
    base = dict(data_dir=dataset, cursor_secret="t" * 32, rate_per_minute=60, rate_burst=BURST)
    app = create_app(Settings(**{**base, **settings}))
    asgi = app
    if unix:
        async def asgi(scope, receive, send):
            if scope["type"] == "http":
                scope = {**scope, "client": None, "server": ("/run/api.sock", None)}
            await app(scope, receive, send)
    client = TestClient(asgi, client=peer, base_url="http://testserver")
    client.app_under_test = app
    return client


@pytest.fixture
def make(dataset):
    made = []

    def factory(**kw):
        client = build(dataset, **kw)
        made.append(client)
        return client
    yield factory
    for client in made:
        client.app_under_test.state.db.close()


def run(client, n=BURST + 3, headers=None):
    """The status of n requests in a row to a counted route."""
    return [client.get("/v1/meta", headers=headers or {}).status_code for _ in range(n)]


LIMITED = [200] * BURST + [429] * 3


def policy(**settings):
    return clients.Policy.from_settings(Settings(**settings))


# Making an address canonical -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, key", [
    ("203.0.113.5", "203.0.113.5"), ("::ffff:203.0.113.5", "203.0.113.5"), ("::FFFF:CB00:7105", "203.0.113.5"),
    ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"), ("2001:DB8:0001:0002::1", "2001:db8:1:2::/64"), ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("2001:0db8:0001:0003::1", "2001:db8:1:3::/64"), ("::1", "::/64"), ("fe80::1", "fe80::/64")])
def test_an_address_has_one_canonical_key(text, key):
    p = policy()
    assert p.key(p.parse(text)) == key


@pytest.mark.parametrize("text", ["", " ", "203.0.113", "203.0.113.256", "203.0.113.5:80", "[2001:db8::1]", "2001:db8::1%eth0", "localhost", "unknown", "203.0.113.5/32", "0x7f.0.0.1", "127.1",
                                  "1" * 40, "2001:db8::1 ", "２０３.０.１１３.５", "203.0.113.5\x00", "203.0.113.5\n", "-1", "::g"])
def test_a_text_that_is_not_an_address_is_not_accepted(text):
    assert policy().parse(text) is None


@pytest.mark.parametrize("prefix, a, b, same", [(64, "2001:db8:1:2::1", "2001:db8:1:2:9::7", True), (64, "2001:db8:1:2::1", "2001:db8:1:3::1", False),
                                                (56, "2001:db8:1:2::1", "2001:db8:1:3::1", True), (56, "2001:db8:1:2::1", "2001:db8:2:2::1", False),
                                                (128, "2001:db8:1:2::1", "2001:db8:1:2::2", False)])
def test_the_ipv6_prefix_sets_what_counts_as_one_client(prefix, a, b, same):
    p = policy(ipv6_prefix=prefix)
    assert (p.key(p.parse(a)) == p.key(p.parse(b))) is same


def test_every_key_is_short():
    p = policy()
    assert max(len(p.key(p.parse(t))) for t in ("2001:db8:ffff:ffff:ffff:ffff:ffff:ffff", "255.255.255.255", "::ffff:255.255.255.255")) <= 45


# Peer mode ---------------------------------------------------------------------------------------------------------------

def test_in_peer_mode_every_forwarding_header_is_ignored(make):
    client = make()
    statuses = []
    for i in range(BURST + 3):
        statuses.append(client.get("/v1/meta", headers={"X-Forwarded-For": f"198.51.100.{i}", "CF-Connecting-IP": f"198.51.100.{i}", "X-Real-IP": f"198.51.100.{i}",
                                                       "Forwarded": f"for=198.51.100.{i}"}).status_code)
    assert statuses == LIMITED


def test_in_peer_mode_two_connections_are_two_clients(make):
    one, two = make(peer=("203.0.113.1", 1)), make(peer=("203.0.113.2", 1))
    assert run(one) == LIMITED and run(two) == LIMITED


def test_in_peer_mode_ipv6_peers_in_one_prefix_share_a_limit(make):
    assert run(make(peer=("2001:db8:1:2::1", 1)), n=2) == [200, 200]
    assert run(make(peer=("2001:db8:1:2:aaaa::9", 1)), n=2) == [200, 200]        # another app, so another limiter: the sharing is shown below on one app


def test_in_peer_mode_ipv6_peers_in_one_prefix_share_one_bucket_on_one_app(dataset):
    one = build(dataset, peer=("2001:db8:1:2::1", 1))
    two = TestClient(one.app_under_test, client=("2001:db8:1:2:aaaa::9", 1), base_url="http://testserver")
    three = TestClient(one.app_under_test, client=("2001:db8:1:3::1", 1), base_url="http://testserver")
    try:
        assert run(one, 2) == [200, 200] and run(two, 4) == [200, 429, 429, 429]
        assert run(three, 3) == [200, 200, 200], "a different prefix has its own limit"
        assert one.app_under_test.state.limiter.clients() == 2
    finally:
        one.app_under_test.state.db.close()


def test_in_peer_mode_a_peer_that_is_not_an_address_is_kept_apart_and_cut_short(make):
    client = make(peer=("testclient", 5))
    assert run(client) == LIMITED
    long = make(peer=("x" * 5000, 5))
    run(long, 1)
    assert max(len(k) for k in long.app_under_test.state.limiter._buckets) <= clients.PEER_KEY_CHARS + len("opaque:")


def test_in_peer_mode_a_connection_with_no_address_is_refused_not_pooled(make):
    client = make(unix=True)
    for _ in range(BURST + 3):
        response = client.get("/v1/meta")
        assert response.status_code == 500 and response.json()["detail"] == "Something went wrong", "refused every time, never a shared bucket that fills"
    assert client.app_under_test.state.limiter.clients() == 0
    assert client.get("/health").status_code == 200, "liveness needs no address"


# Proxy mode ----------------------------------------------------------------------------------------------------------------

PROXY = dict(client_ip="proxy", trusted_proxies="198.51.100.0/24, 10.0.0.0/8, 2001:db8:aa::/48")


def test_a_header_from_an_unlisted_peer_is_ignored_even_when_it_is_a_valid_address(make):
    client = make(peer=("203.0.113.9", 1), **PROXY)
    statuses = [client.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code for i in range(BURST + 3)]
    assert statuses == LIMITED


def test_a_header_from_an_unlisted_peer_is_ignored_even_when_it_is_garbage(make):
    client = make(peer=("203.0.113.9", 1), **PROXY)
    assert [client.get("/v1/meta", headers={"X-Forwarded-For": "not, an, address"}).status_code for _ in range(BURST + 1)] == [200] * BURST + [429]


def test_a_header_from_a_listed_peer_names_the_client(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    one = [client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.1"}).status_code for _ in range(BURST + 2)]
    two = [client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.2"}).status_code for _ in range(BURST + 2)]
    assert one == two == [200] * BURST + [429] * 2


def test_the_chain_is_read_from_the_right_and_a_client_cannot_write_its_own_address(make):
    """The proxy appends the address it saw, so what the client put to the left is never read, and a client cannot get a fresh limit by changing it."""
    client = make(peer=("198.51.100.7", 1), **PROXY)
    seen = {"192.0.2.50": 0}
    for i in range(BURST + 3):
        spoof = f"203.0.113.{i}, 192.0.2.50"
        status = client.get("/v1/meta", headers={"X-Forwarded-For": spoof}).status_code
        assert status == (200 if i < BURST else 429), i
    assert client.app_under_test.state.limiter.clients() == 1


def test_listed_proxies_in_the_chain_are_skipped(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    for i in range(BURST):
        client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.9, 10.1.2.3, 198.51.100.200"})
    refused = client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.9"})
    assert refused.status_code == 429, "a client behind two listed proxies is the same client as one behind none"
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.10, 10.1.2.3"}).status_code == 200


def test_when_every_hop_is_a_listed_proxy_the_leftmost_is_the_client(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    for _ in range(BURST):
        client.get("/v1/meta", headers={"X-Forwarded-For": "10.9.9.9, 10.1.1.1"})
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "10.9.9.9"}).status_code == 429


def test_a_listed_peer_with_no_header_is_its_own_client(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    assert run(client) == LIMITED
    assert list(client.app_under_test.state.limiter._buckets) == ["198.51.100.7"]


def test_repeated_header_lines_are_one_list(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    for _ in range(BURST):
        client.get("/v1/meta", headers=[("X-Forwarded-For", "192.0.2.77"), ("X-Forwarded-For", "10.0.0.1")])
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.77"}).status_code == 429


def test_ipv6_clients_behind_a_proxy_are_grouped_by_prefix(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    for i in range(BURST):
        client.get("/v1/meta", headers={"X-Forwarded-For": f"2001:db8:7:7:{i}::1"})
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "2001:db8:7:7:ffff::5"}).status_code == 429
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "2001:db8:7:8::5"}).status_code == 200


def test_an_ipv6_peer_in_a_listed_ipv6_network_is_a_listed_proxy(make):
    client = make(peer=("2001:db8:aa::5", 1), **PROXY)
    for _ in range(BURST):
        client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.3"})
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.3"}).status_code == 429


@pytest.mark.parametrize("value", ["garbage", "192.0.2.1, ", ", 192.0.2.1", "192.0.2.1,,10.0.0.1", "192.0.2.1:8080", "[2001:db8::1]:80", "192.0.2.1, unknown", "unknown", "for=192.0.2.1",
                                   "192.0.2.1;evil", "192.0.2.1\t,x", "2001:db8::1%eth0", "192.0.2.1 192.0.2.2", "192.0.2.256", "0xC0.0.2.1", "a" * 2000,
                                   ", ".join(f"192.0.2.{i}" for i in range(1, 40))])
def test_a_header_from_a_listed_peer_that_is_not_a_clean_list_is_refused(make, value):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    response = client.get("/v1/meta", headers={"X-Forwarded-For": value})
    assert response.status_code == 400 and response.headers["content-type"].startswith("application/problem+json")
    assert value[:30] not in response.text or value == "garbage", "the header is not echoed"
    assert client.app_under_test.state.limiter.clients() == 0, "a refused request costs the limiter no memory"


def test_the_header_name_can_be_chosen(make):
    client = make(peer=("198.51.100.7", 1), client_ip_header="CF-Connecting-IP", **PROXY)
    for _ in range(BURST):
        client.get("/v1/meta", headers={"CF-Connecting-IP": "192.0.2.5", "X-Forwarded-For": "192.0.2.6"})
    assert client.get("/v1/meta", headers={"CF-Connecting-IP": "192.0.2.5"}).status_code == 429
    assert client.get("/v1/meta", headers={"CF-Connecting-IP": "192.0.2.6"}).status_code == 200
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "not an address"}).status_code == 200, "the other header is not read at all"


def test_health_needs_no_address_and_is_not_refused_for_a_bad_header(make):
    client = make(peer=("198.51.100.7", 1), **PROXY)
    assert client.get("/health", headers={"X-Forwarded-For": "garbage"}).status_code == 200


# Unix sockets --------------------------------------------------------------------------------------------------------------

def test_a_unix_socket_in_proxy_mode_takes_the_client_from_the_header(make):
    client = make(unix=True, client_ip="proxy", trusted_proxies="unix")
    one = [client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.1"}).status_code for _ in range(BURST + 2)]
    two = [client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.2"}).status_code for _ in range(BURST + 2)]
    assert one == two == [200] * BURST + [429] * 2


def test_a_unix_socket_request_without_a_header_is_the_local_client(make):
    client = make(unix=True, client_ip="proxy", trusted_proxies="unix")
    assert run(client) == LIMITED and list(client.app_under_test.state.limiter._buckets) == ["local"]


def test_a_unix_socket_with_only_addresses_listed_is_refused(make):
    client = make(unix=True, client_ip="proxy", trusted_proxies="10.0.0.0/8")
    assert client.get("/v1/meta", headers={"X-Forwarded-For": "192.0.2.1"}).status_code == 500


def test_listing_unix_does_not_make_tcp_peers_trusted(make):
    client = make(peer=("203.0.113.9", 1), client_ip="proxy", trusted_proxies="unix")
    assert [client.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code for i in range(BURST + 2)] == [200] * BURST + [429] * 2


def test_the_refusal_is_logged_once(make, caplog):
    client = make(unix=True)
    with caplog.at_level("WARNING", logger="cricket_api.clients"):
        for _ in range(5):
            client.get("/v1/meta")
    assert [r.getMessage() for r in caplog.records if "no client address" in r.getMessage()].__len__() == 1


# Settings --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("settings", [
    dict(client_ip="proxy"), dict(client_ip="proxy", trusted_proxies=" , "), dict(client_ip="proxy", trusted_proxies="not-an-address"), dict(client_ip="proxy", trusted_proxies="10.0.0.1/8"),
    dict(client_ip="proxy", trusted_proxies="0.0.0.0/0"), dict(client_ip="proxy", trusted_proxies="::/0"), dict(client_ip="proxy", trusted_proxies="127.0.0.1, 0.0.0.0/0"),
    dict(client_ip="proxy", trusted_proxies="127.0.0.1", client_ip_header="X Forwarded"), dict(client_ip="proxy", trusted_proxies="127.0.0.1", client_ip_header=""),
    dict(client_ip="proxy", trusted_proxies="127.0.0.1", client_ip_header="X-Forwarded-For\r\nX: 1"), dict(client_ip="Proxy", trusted_proxies="127.0.0.1"), dict(client_ip="header"),
    dict(client_ip="peer", trusted_proxies="127.0.0.1"), dict(ipv6_prefix=16), dict(ipv6_prefix=129), dict(max_clients=0)])
def test_a_setting_that_cannot_work_stops_the_start(dataset, settings):
    with pytest.raises(clients.ConfigError):
        create_app(Settings(data_dir=dataset, cursor_secret="t" * 32, **settings))


def test_the_old_cloudflare_setting_is_refused_with_a_pointer_to_the_new_ones(dataset, monkeypatch):
    monkeypatch.setenv("CRICKET_API_BEHIND_CLOUDFLARE", "1")
    with pytest.raises(clients.ConfigError, match="CRICKET_API_TRUSTED_PROXIES"):
        create_app(Settings(data_dir=dataset, cursor_secret="t" * 32))


def test_the_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("CRICKET_API_CLIENT_IP", "proxy")
    monkeypatch.setenv("CRICKET_API_TRUSTED_PROXIES", "10.0.0.0/8,unix")
    monkeypatch.setenv("CRICKET_API_CLIENT_IP_HEADER", "CF-Connecting-IP")
    monkeypatch.setenv("CRICKET_API_IPV6_PREFIX", "56")
    p = clients.Policy.from_settings(Settings())
    assert (p.mode, p.unix, p.header, p.ipv6_prefix, len(p.networks)) == ("proxy", True, "cf-connecting-ip", 56, 1)


def test_the_default_is_peer_mode_that_trusts_nothing():
    p = clients.Policy.from_settings(Settings())
    assert (p.mode, p.networks, p.unix) == ("peer", (), False)


def test_the_documentation_tells_how_to_start_the_server_and_set_up_a_proxy():
    readme = (Path(__file__).parent.parent / "docs" / "reference.md").read_text()
    assert "python -m cricket_api" in readme and "CRICKET_API_TRUSTED_PROXIES" in readme and "CRICKET_API_IPV6_PREFIX" in readme and "CRICKET_API_BEHIND_CLOUDFLARE" not in readme.replace("no longer", "")


# Real transports -----------------------------------------------------------------------------------------------------------

class Server:
    def __init__(self, env, uds=None, port=None, extra=()):
        self.dir = tempfile.mkdtemp(dir="/tmp")                    # a unix socket path must be short
        self.uds = os.path.join(self.dir, "api.sock") if uds else None
        self.port = port
        base = {**os.environ, "CRICKET_API_DATA": str(GOLDEN), "CRICKET_API_SECRET": "x" * 32, "CRICKET_API_RATE": "60", "CRICKET_API_BURST": str(BURST)}
        for key in [k for k in base if k.startswith("CRICKET_API_") and k not in ("CRICKET_API_DATA", "CRICKET_API_SECRET", "CRICKET_API_RATE", "CRICKET_API_BURST")]:
            del base[key]
        where = ["--uds", self.uds] if uds else ["--port", str(port)]
        self.proc = subprocess.Popen([sys.executable, "-m", "cricket_api", *where, "--no-access-log", *extra],
                                     env={**base, **env}, cwd=Path(__file__).parent.parent, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("the server stopped: " + self.proc.stderr.read().decode()[-500:])
            try:
                self.client().get("/health").raise_for_status()
                return
            except (httpx.HTTPError, OSError):
                time.sleep(0.2)
        raise RuntimeError("the server did not start")

    def client(self):
        if self.uds:
            return httpx.Client(transport=httpx.HTTPTransport(uds=self.uds), base_url="http://x", timeout=10)
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10)

    def stop(self):
        self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def server():
    started = []

    def start(env, **kw):
        s = Server(env, **kw)
        started.append(s)
        return s
    yield start
    for s in started:
        s.stop()


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def real_run(server, headers=None, n=BURST + 3):
    with server.client() as c:
        return [c.get("/v1/meta", headers=headers or {}).status_code for _ in range(n)]


def test_a_real_unix_socket_in_proxy_mode_tells_clients_apart_by_the_header(server):
    s = server({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "unix"}, uds=True)
    assert real_run(s, {"X-Forwarded-For": "192.0.2.1"}) == LIMITED
    assert real_run(s, {"X-Forwarded-For": "192.0.2.2"}) == LIMITED, "a second client has its own limit, which the shared bucket used to take away"
    assert real_run(s, {"X-Forwarded-For": "192.0.2.1"}, n=1) == [429]


def test_a_real_tcp_server_ignores_a_forwarding_header_from_a_peer_it_does_not_trust(server):
    """The bypass this replaces: a loopback caller rotated X-Forwarded-For and was never limited. Here loopback is not a listed proxy, so the header is ignored."""
    s = server({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "10.0.0.0/8"}, port=free_port())
    with s.client() as c:
        assert [c.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code for i in range(BURST + 3)] == LIMITED


def test_a_real_tcp_server_in_peer_mode_ignores_forwarding_headers(server):
    s = server({}, port=free_port())
    with s.client() as c:
        assert [c.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}", "CF-Connecting-IP": f"192.0.2.{i}"}).status_code for i in range(BURST + 3)] == LIMITED


def test_a_real_tcp_server_behind_a_listed_loopback_proxy_reads_the_chain(server):
    s = server({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "127.0.0.1"}, port=free_port())
    assert real_run(s, {"X-Forwarded-For": "1.1.1.1, 192.0.2.8"}) == LIMITED
    assert real_run(s, {"X-Forwarded-For": "2.2.2.2, 192.0.2.8"}, n=1) == [429], "the spoofed part on the left changed nothing"
    assert real_run(s, {"X-Forwarded-For": "192.0.2.9"}, n=1) == [200]


def test_a_real_server_with_a_bad_setting_does_not_start(server):
    with pytest.raises(RuntimeError, match="stopped"):
        server({"CRICKET_API_CLIENT_IP": "proxy"}, port=free_port())
