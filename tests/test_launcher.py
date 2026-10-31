"""The launcher (python -m cricket_api) is the supported way to start the server, so it is the thing under test: the options it builds, and the real server it starts over TCP and over a unix socket.

What it has to guarantee: uvicorn's own forwarding header handling is off, there is one worker and no Server header, the address is loopback unless the operator says otherwise, and no argument can change
those. The behaviour that depends on them is checked on the running server: rotating forwarding headers cannot get around the limit in peer mode, and a configured trusted proxy still tells clients apart.
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from cricket_api import serve

GOLDEN = Path(__file__).parent / "golden" / "api"
API = Path(__file__).parent.parent
BURST = 3
LIMITED = [200] * BURST + [429] * 3


# The options it builds ---------------------------------------------------------------------------------------------------

SAFE = {"proxy_headers": False, "workers": 1, "server_header": False, "reload": False}


def test_the_defaults_are_loopback_and_the_safety_settings():
    options = serve.uvicorn_options([])
    assert options["host"] == "127.0.0.1" and options["port"] == serve.DEFAULT_PORT
    assert {k: options[k] for k in SAFE} == SAFE and options["access_log"] is True


@pytest.mark.parametrize("argv", [["--port", "9000"], ["--host", "::1"], ["--host", "localhost", "--port", "1"], ["--uds", "/run/cricket.sock"], ["--no-access-log"], ["--host", "127.0.0.2"],
                                  ["--allow-non-loopback", "--host", "0.0.0.0"], ["--allow-non-loopback", "--host", "192.0.2.1", "--port", "65535"]])
def test_no_choice_that_is_offered_changes_a_safety_setting(argv):
    options = serve.uvicorn_options(argv)
    assert {k: options[k] for k in SAFE} == SAFE


@pytest.mark.parametrize("argv", [["--workers", "4"], ["--workers=2"], ["--proxy-headers"], ["--no-proxy-headers"], ["--forwarded-allow-ips", "*"], ["--forwarded-allow-ips=127.0.0.1"],
                                  ["--server-header"], ["--no-server-header"], ["--reload"], ["--log-level", "debug"], ["--app", "other:app"], ["--factory"], ["--limit-concurrency", "5"],
                                  ["--ssl-keyfile", "x"], ["--interface", "wsgi"], ["--root-path", "/x"], ["--proxy"], ["--work", "4"], ["--no-proxy", "x"], ["--forward", "*"]])
def test_an_option_that_could_override_a_safety_setting_does_not_exist(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        serve.uvicorn_options(argv)
    assert stop.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--host", "0.0.0.0"], ["--host", "::"], ["--host", "192.0.2.1"], ["--host", "example.org"], ["--host", ""], ["--host", "127.0.0.1.evil.example"]])
def test_a_non_loopback_address_needs_an_explicit_flag(argv, capsys):
    with pytest.raises(SystemExit) as stop:
        serve.uvicorn_options(argv)
    assert stop.value.code == 2 and "--allow-non-loopback" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--port", "0"], ["--port", "65536"], ["--port", "-1"], ["--port", "x"], ["--uds", "/a.sock", "--port", "9"], ["--uds", "/a.sock", "--host", "127.0.0.1"],
                                  ["--uds", "/a.sock", "--port=9"], ["--uds", "/a.sock", "--host=127.0.0.1"]])
def test_a_bad_port_or_a_socket_combined_with_an_address_is_refused(argv):
    with pytest.raises(SystemExit) as stop:
        serve.uvicorn_options(argv)
    assert stop.value.code == 2


def test_a_unix_socket_option_has_no_host_or_port():
    options = serve.uvicorn_options(["--uds", "/run/cricket.sock"])
    assert options["uds"] == "/run/cricket.sock" and "host" not in options and "port" not in options


def test_loopback_names():
    assert all(serve.is_loopback(h) for h in ("127.0.0.1", "127.9.9.9", "::1", "localhost", "LOCALHOST", "::ffff:127.0.0.1"))
    assert not any(serve.is_loopback(h) for h in ("0.0.0.0", "::", "10.0.0.1", "", "localhost.example", "127.0.0.1.example"))


def test_the_command_and_the_module_are_both_installed():
    assert 'cricket-api = "cricket_api.serve:main"' in (API / "pyproject.toml").read_text()
    assert (API / "cricket_api" / "__main__.py").exists()


# The real server -------------------------------------------------------------------------------------------------------------

class Launched:
    def __init__(self, env=None, argv=(), uds=False):
        self.dir = tempfile.mkdtemp(dir="/tmp")                    # a unix socket path must be short
        self.uds = os.path.join(self.dir, "api.sock") if uds else None
        if not uds:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
        base = {k: v for k, v in os.environ.items() if not k.startswith(("CRICKET_API_", "UVICORN_"))}
        base.update({"CRICKET_API_DATA": str(GOLDEN), "CRICKET_API_SECRET": "x" * 32, "CRICKET_API_RATE": "60", "CRICKET_API_BURST": str(BURST)})
        for key, value in (env or {}).items():
            if value is None:                                      # None removes a variable, to start a server with the setting absent
                base.pop(key, None)
            else:
                base[key] = value
        where = ["--uds", self.uds] if uds else ["--port", str(self.port)]
        self.log = open(os.path.join(self.dir, "log.txt"), "w+")
        self.proc = subprocess.Popen([sys.executable, "-m", "cricket_api", *where, "--no-access-log", *argv], env=base, cwd=API, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                stopped = Stopped(self.output(), self.proc.returncode, bool(self.uds and os.path.exists(self.uds)))
                self.log.close()
                shutil.rmtree(self.dir, ignore_errors=True)
                raise stopped
            try:
                self.client().get("/health").raise_for_status()
                return
            except (httpx.HTTPError, OSError):
                time.sleep(0.2)
        raise RuntimeError("the server did not start")

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def client(self):
        if self.uds:
            return httpx.Client(transport=httpx.HTTPTransport(uds=self.uds), base_url="http://x", timeout=10)
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=10)

    def run(self, headers=None, n=BURST + 3, path="/v1/meta"):
        with self.client() as c:
            return [c.get(path, headers=headers or {}).status_code for _ in range(n)]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Stopped(RuntimeError):
    def __init__(self, output, code, socket_made=False):
        super().__init__(f"the launcher stopped with {code}: {output[-600:]}")
        self.output, self.code, self.socket_made = output, code, socket_made

    def never_started_uvicorn(self):
        """No listening line, none of uvicorn's own startup lines, and no socket file."""
        return not any(text in self.output for text in ("listening on", "Started server process", "Uvicorn running", "Waiting for application startup")) and not self.socket_made


@pytest.fixture
def launch():
    started = []

    def start(env=None, **kw):
        server = Launched(env, **kw)
        started.append(server)
        return server
    yield start
    for server in started:
        server.stop()


def test_the_launcher_starts_a_working_server_on_loopback_with_no_server_header(launch):
    server = launch()
    with server.client() as c:
        response = c.get("/v1/meta")
    assert response.status_code == 200 and "server" not in response.headers
    assert f"127.0.0.1:{server.port}" in server.output() and "client identity peer" in server.output()
    # reachable on loopback and not on another local address: an address that is not loopback must not answer
    other = [a for a in _local_addresses() if not a.startswith("127.")]
    for address in other[:1]:
        with pytest.raises(OSError):
            socket.create_connection((address, server.port), 1).close()


def _local_addresses():
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(found)


def test_peer_mode_over_tcp_rotating_forwarding_headers_cannot_bypass_the_limit(launch):
    """The bypass: a loopback caller sent a different X-Forwarded-For each time and uvicorn's own handling made each one a new client."""
    server = launch()
    statuses = server.run(n=BURST + 3, headers=None)
    assert statuses == LIMITED
    server2 = launch()
    with server2.client() as c:
        rotating = [c.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}", "X-Real-IP": f"192.0.2.{i}", "CF-Connecting-IP": f"192.0.2.{i}", "Forwarded": f"for=192.0.2.{i}"}).status_code
                    for i in range(BURST + 3)]
    assert rotating == LIMITED


def test_a_listed_proxy_over_tcp_still_tells_clients_apart_and_ignores_what_a_client_wrote(launch):
    server = launch({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "127.0.0.1"})
    assert server.run({"X-Forwarded-For": "192.0.2.1"}) == LIMITED
    assert server.run({"X-Forwarded-For": "192.0.2.2"}) == LIMITED, "a second client has its own allowance"
    assert server.run({"X-Forwarded-For": "1.1.1.1, 192.0.2.1"}, n=1) == [429], "what a client wrote to the left of the proxy's entry changes nothing"
    assert server.run({"X-Forwarded-For": "not an address"}, n=1) == [400]


def test_a_proxy_that_is_not_listed_cannot_name_a_client_over_tcp(launch):
    server = launch({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "10.0.0.0/8"})
    with server.client() as c:
        assert [c.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code for i in range(BURST + 3)] == LIMITED


def test_the_supported_unix_socket_setup_tells_clients_apart_by_the_header(launch):
    server = launch({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "unix"}, uds=True)
    assert server.run({"X-Forwarded-For": "192.0.2.1"}) == LIMITED
    assert server.run({"X-Forwarded-For": "192.0.2.2"}) == LIMITED
    assert server.run({"X-Forwarded-For": "1.1.1.1, 192.0.2.1"}, n=1) == [429]
    assert server.run(n=BURST + 1) == [200] * BURST + [429], "a request with no header is the client local"
    with server.client() as c:
        assert "server" not in c.get("/health").headers


def test_a_unix_socket_in_peer_mode_does_not_start(launch):
    with pytest.raises(Stopped) as stop:
        launch({}, uds=True)
    assert stop.value.code == 2 and "CRICKET_API_TRUSTED_PROXIES to include unix" in stop.value.output and "Traceback" not in stop.value.output
    assert stop.value.never_started_uvicorn()


@pytest.mark.parametrize("env, text", [({"CRICKET_API_CLIENT_IP": "proxy"}, "needs CRICKET_API_TRUSTED_PROXIES"), ({"CRICKET_API_BEHIND_CLOUDFLARE": "1"}, "no longer read"),
                                       ({"CRICKET_API_IPV6_PREFIX": "8"}, "IPV6_PREFIX"), ({"CRICKET_API_DATA": "/nonexistent-folder"}, "meta.json is missing")])
def test_a_setting_that_cannot_work_stops_the_launcher_with_a_message_and_no_traceback(launch, env, text):
    with pytest.raises(Stopped) as stop:
        launch(env)
    assert stop.value.code == 2 and text in stop.value.output and "Traceback" not in stop.value.output


def test_a_bad_argument_stops_the_launcher_before_anything_starts(launch):
    with pytest.raises(Stopped) as stop:
        launch(argv=["--workers", "4"])
    assert stop.value.code == 2 and "unrecognized arguments" in stop.value.output


def test_the_environment_cannot_switch_uvicorn_s_own_settings_back_on(launch):
    """uvicorn reads UVICORN_* variables when it is started from its own command. The launcher does not use that path, so they change nothing."""
    server = launch({"UVICORN_PROXY_HEADERS": "1", "UVICORN_FORWARDED_ALLOW_IPS": "*", "UVICORN_WORKERS": "4", "UVICORN_SERVER_HEADER": "1"})
    with server.client() as c:
        rotating = [c.get("/v1/meta", headers={"X-Forwarded-For": f"192.0.2.{i}"}).status_code for i in range(BURST + 3)]
        assert rotating == LIMITED and "server" not in c.get("/health").headers
    children = subprocess.run(["pgrep", "-P", str(server.proc.pid)], capture_output=True, text=True).stdout.split()
    assert children == [], "one process, no worker children"


def test_the_readme_startup_command_is_the_launcher():
    readme = (API / "docs" / "reference.md").read_text()
    assert "python -m cricket_api" in readme
    assert "uvicorn cricket_api.main:app" not in readme.replace("`uvicorn cricket_api.main:app`", "")


# Settings that cannot work: the launcher stops before uvicorn is touched ----------------------------------------------------

@pytest.mark.parametrize("env", [{"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "10.0.0.0/8"}, {"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "127.0.0.1, ::1"}])
def test_a_unix_socket_in_proxy_mode_without_unix_trust_does_not_start(launch, env):
    with pytest.raises(Stopped) as stop:
        launch(env, uds=True)
    assert stop.value.code == 2 and "to include unix" in stop.value.output and "Traceback" not in stop.value.output
    assert stop.value.never_started_uvicorn()


def test_a_unix_socket_with_unix_trust_next_to_addresses_starts(launch):
    server = launch({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "10.0.0.0/8, unix"}, uds=True)
    assert server.run({"X-Forwarded-For": "192.0.2.1"}) == LIMITED


@pytest.mark.parametrize("variable, value", [
    ("CRICKET_API_THREADS", "abc"), ("CRICKET_API_THREADS", ""), ("CRICKET_API_THREADS", "2.5"), ("CRICKET_API_CONCURRENCY", "four"), ("CRICKET_API_RATE", "sixty"), ("CRICKET_API_RATE", "1e3"),
    ("CRICKET_API_BURST", "x"), ("CRICKET_API_IPV6_PREFIX", "sixty four"), ("CRICKET_API_MAX_CLIENTS", "10,000"),
    ("CRICKET_API_QUERY_SECONDS", "soon"), ("CRICKET_API_QUERY_SECONDS", ""), ("CRICKET_API_QUEUE_SECONDS", "1.5s"), ("CRICKET_API_QUEUE_SECONDS", "nan"), ("CRICKET_API_QUERY_SECONDS", "inf")])
def test_a_malformed_number_stops_the_real_launcher_with_a_message_and_exit_code_2(launch, variable, value):
    with pytest.raises(Stopped) as stop:
        launch({variable: value})
    assert stop.value.code == 2, stop.value.output
    assert variable in stop.value.output and "Traceback" not in stop.value.output and "ValueError" not in stop.value.output
    assert stop.value.never_started_uvicorn()


def test_a_malformed_number_over_a_unix_socket_stops_too(launch):
    with pytest.raises(Stopped) as stop:
        launch({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "unix", "CRICKET_API_RATE": "fast"}, uds=True)
    assert stop.value.code == 2 and "CRICKET_API_RATE" in stop.value.output and stop.value.never_started_uvicorn()


class FakeUvicorn:
    """Stands in for uvicorn.run, so that a test can say for certain that it was or was not reached."""
    def __init__(self):
        self.calls = []

    def run(self, app, **options):
        self.calls.append(options)


@pytest.fixture
def fake_uvicorn(monkeypatch):
    import uvicorn
    fake = FakeUvicorn()
    monkeypatch.setattr(uvicorn, "run", fake.run)
    for key in [k for k in os.environ if k.startswith("CRICKET_API_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("CRICKET_API_DATA", str(GOLDEN))
    return fake


@pytest.mark.parametrize("env, argv", [
    ({"CRICKET_API_RATE": "abc"}, []), ({"CRICKET_API_QUERY_SECONDS": "nan"}, []), ({"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "10.0.0.0/8"}, ["--uds", "/tmp/x.sock"]),
    ({}, ["--uds", "/tmp/x.sock"]), ({"CRICKET_API_CLIENT_IP": "proxy"}, []), ({"CRICKET_API_DATA": "/nonexistent-folder"}, [])])
def test_uvicorn_is_never_reached_when_a_setting_cannot_work(fake_uvicorn, monkeypatch, capsys, env, argv):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert serve.main(argv) == 2
    assert fake_uvicorn.calls == [] and "cricket-api:" in capsys.readouterr().err


def test_uvicorn_is_reached_with_the_safety_settings_when_everything_is_fine(fake_uvicorn, monkeypatch):
    assert serve.main(["--port", "9"]) == 0
    options = fake_uvicorn.calls[0]
    assert {k: options[k] for k in SAFE} == SAFE and options["host"] == "127.0.0.1"
    monkeypatch.setenv("CRICKET_API_CLIENT_IP", "proxy")
    monkeypatch.setenv("CRICKET_API_TRUSTED_PROXIES", "unix")
    assert serve.main(["--uds", "/tmp/x.sock"]) == 0 and fake_uvicorn.calls[1]["uds"] == "/tmp/x.sock"


def test_a_fault_in_the_program_is_not_hidden_as_a_configuration_error(fake_uvicorn, monkeypatch):
    """The handling is narrow: only ConfigError and DataError are caught. A TypeError or a ValueError from anywhere else keeps its traceback."""
    import cricket_api.app as app_module

    def broken(settings):
        raise TypeError("a bug")
    monkeypatch.setattr(app_module, "create_app", broken)
    with pytest.raises(TypeError, match="a bug"):
        serve.main([])
    monkeypatch.setattr(app_module, "create_app", lambda settings: (_ for _ in ()).throw(ValueError("also a bug")))
    with pytest.raises(ValueError, match="also a bug"):
        serve.main([])
    assert fake_uvicorn.calls == []


def test_only_the_number_settings_raise_the_configuration_error(monkeypatch):
    from cricket_api.settings import ConfigError, Settings, _float, _int
    monkeypatch.setenv("CRICKET_API_RATE", "x")
    with pytest.raises(ConfigError, match="CRICKET_API_RATE must be a whole number"):
        Settings()
    monkeypatch.setenv("CRICKET_API_RATE", "5")
    monkeypatch.setenv("CRICKET_API_QUEUE_SECONDS", "-")
    with pytest.raises(ConfigError, match="CRICKET_API_QUEUE_SECONDS must be a number"):
        Settings()
    monkeypatch.delenv("CRICKET_API_QUEUE_SECONDS")
    assert _int("CRICKET_API_NOT_SET", 7) == 7 and _float("CRICKET_API_NOT_SET", 1.5) == 1.5
    with pytest.raises(TypeError):
        _int("CRICKET_API_NOT_SET", None)                   # not a value error, so not turned into a configuration message
