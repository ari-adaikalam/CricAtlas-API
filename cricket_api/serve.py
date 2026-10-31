"""The supported way to start the server: `python -m cricket_api` (or the `cricket-api` command).

It runs uvicorn with the settings the API depends on, and offers no option that changes them:

  * proxy_headers is off. uvicorn's own handling of X-Forwarded-For would rewrite the connection address before the API sees it, and the API reads forwarding headers itself, under
    CRICKET_API_TRUSTED_PROXIES (see clients.py). Left on, a caller on the loopback address could pick its own rate limit key.
  * one worker, because the rate limiter keeps its state in the memory of one process.
  * no Server header.
  * a limit on connections (limit_concurrency, CRICKET_API_MAX_CONNECTIONS, 100 or more as the admission rooms need). It bounds what the process holds before the admission gate is reached. It is a guard,
    not a measured capacity, and idle connections count against it, so it is meant for a server behind a proxy or CDN that manages connections.
  * the address is loopback unless --allow-non-loopback is given, since the API expects a proxy or a firewall in front of it.

What can be chosen is where to listen (--host and --port, or --uds), whether to write an access log, and whether to run the private metrics listener (--metrics-port). Everything else
is read from the CRICKET_API_* settings.

The metrics listener is off unless --metrics-port is given. It is a second server in the same process, on its own address, that serves GET /metrics and nothing else (see telemetry.py). It has no login,
so where it listens is the control: loopback unless --allow-non-loopback-metrics is given, never a wildcard address (0.0.0.0 or ::), never the public server's own address, and an address given as
an IP number or localhost, not a name. In a container, give it the container's address on a network that only the monitor is on, and publish no port for it.
A setting that cannot work stops the start with a message, not a traceback.
"""
import argparse
import ipaddress
import logging
import os
import sys

from cricket_api import admission

LOOPBACK_NAMES = ("localhost",)
DEFAULT_PORT = 8000


def is_loopback(host: str) -> bool:
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cricket-api", description="Start the Cricket API. One worker, no forwarding header handling in uvicorn, no Server header. See the README.", allow_abbrev=False)
    where = p.add_mutually_exclusive_group()
    where.add_argument("--uds", metavar="PATH", help="listen on a unix socket (needs CRICKET_API_CLIENT_IP=proxy and CRICKET_API_TRUSTED_PROXIES=unix)")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})")
    p.add_argument("--allow-non-loopback", action="store_true", help="allow --host to be an address other than loopback")
    p.add_argument("--no-access-log", action="store_true", help="do not write a log line for every request")
    p.add_argument("--metrics-port", type=int, default=None, help="run the private metrics listener on this port (off when not given)")
    p.add_argument("--metrics-host", default="127.0.0.1", help="address of the metrics listener (default 127.0.0.1). A specific address, never 0.0.0.0 or ::")
    p.add_argument("--allow-non-loopback-metrics", action="store_true", help="allow --metrics-host to be an address other than loopback, for a network only the monitor can reach")
    return p


def uvicorn_options(argv: list[str], connections: int = admission.DEFAULT_CONNECTIONS) -> dict:
    """The arguments for uvicorn. The safety settings are written here and no argument can change them. `connections` comes from the checked settings (admission.Limits)."""
    p = parser()
    args = p.parse_args(argv)
    if args.uds:
        if "--host" in argv or "--port" in argv or any(a.startswith(("--host=", "--port=")) for a in argv):
            p.error("--uds cannot be combined with --host or --port")
        where = {"uds": args.uds}
    else:
        if not 1 <= args.port <= 65535:
            p.error("--port must be between 1 and 65535")
        if not is_loopback(args.host) and not args.allow_non_loopback:
            p.error(f"{args.host} is not a loopback address. The API expects a proxy or a firewall in front of it. Pass --allow-non-loopback to listen there anyway")
        where = {"host": args.host, "port": args.port}
    return {**where, "proxy_headers": False, "workers": 1, "server_header": False, "date_header": True, "reload": False, "limit_concurrency": connections, "access_log": not args.no_access_log, "log_level": "info",
            "lifespan": "auto", "interface": "asgi3"}


def metrics_options(argv: list[str]) -> dict | None:
    """Where the private metrics listener goes, or None when it is not asked for. Anything that could expose it more widely than intended stops the start."""
    p = parser()
    args = p.parse_args(argv)
    if args.metrics_port is None:
        if "--metrics-host" in argv or any(a.startswith("--metrics-host=") for a in argv) or args.allow_non_loopback_metrics:
            p.error("--metrics-host and --allow-non-loopback-metrics need --metrics-port")
        return None
    if not 1 <= args.metrics_port <= 65535:
        p.error("--metrics-port must be between 1 and 65535")
    host = args.metrics_host
    try:
        address = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        if host.lower() not in LOOPBACK_NAMES:
            p.error(f"--metrics-host must be an IP address or localhost, not {host[:40]!r}: a name can resolve to something wider than intended")
        address = None
    if address is not None and address.is_unspecified:
        p.error("--metrics-host cannot be a wildcard address (0.0.0.0 or ::): the listener has no login, so it must be given the one address a monitor reaches it on")
    if not is_loopback(host) and not args.allow_non_loopback_metrics:
        p.error(f"{host} is not a loopback address. The metrics listener has no login. Pass --allow-non-loopback-metrics only for an address on a network that nothing but the monitor can reach")
    if not args.uds and args.metrics_port == args.port and _same_address(host, args.host):
        p.error("--metrics-port is the public server's own port on the same address")
    return {"host": host, "port": args.metrics_port}


def _same_address(a: str, b: str) -> bool:
    def plain(host: str) -> str:
        return "127.0.0.1" if host.lower() == "localhost" else host.split("%")[0]
    try:
        return ipaddress.ip_address(plain(a)) == ipaddress.ip_address(plain(b))
    except ValueError:
        return plain(a).lower() == plain(b).lower()


def configure_logging() -> None:
    """The API's own log lines go to stderr with a time and a level, next to uvicorn's. Without this they would be written bare by Python's fallback handler."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("cricket_api")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    uvicorn_options(argv)                                        # a bad argument stops here, with exit code 2, before anything is imported or read
    metrics = metrics_options(argv)
    # Imported here so that --help and a bad argument do not need uvicorn, the data or the settings.
    import uvicorn

    from cricket_api import clients
    from cricket_api.app import create_app
    from cricket_api.db import DataError
    from cricket_api.settings import ConfigError, Settings

    # Only the failures that mean "this configuration cannot work" are caught here. Any other exception is a fault in the program and keeps its traceback.
    try:
        settings = Settings()
        options = uvicorn_options(argv, admission.Limits.from_settings(settings).connections)
        if "uds" in options:
            policy = clients.Policy.from_settings(settings)
            if not (policy.mode == "proxy" and policy.unix):
                raise clients.ConfigError("a unix socket carries no client address, so --uds needs CRICKET_API_CLIENT_IP=proxy and CRICKET_API_TRUSTED_PROXIES to include unix "
                                          "(and the proxy must set the header named by CRICKET_API_CLIENT_IP_HEADER)")
        app = create_app(settings)
    except (ConfigError, DataError) as error:
        print(f"cricket-api: {error}", file=sys.stderr)
        return 2
    configure_logging()
    listener = None
    if metrics:
        from cricket_api.telemetry import Listener, ListenerError
        listener = Listener(app.state.telemetry, metrics["host"], metrics["port"])
        try:
            listener.start()
        except ListenerError as error:                           # before the public server starts, so a half-started process never serves
            print(f"cricket-api: {error}", file=sys.stderr)
            return 2
        print(f"cricket-api: private metrics on {metrics['host']}:{metrics['port']}", file=sys.stderr, flush=True)
    where = options.get("uds") or f"{options['host']}:{options['port']}"
    print(f"cricket-api: listening on {where}, client identity {settings.client_ip}, pid {os.getpid()}", file=sys.stderr, flush=True)
    try:
        uvicorn.run(app, **options)
    finally:
        if listener is not None:
            listener.stop()
    return 0
