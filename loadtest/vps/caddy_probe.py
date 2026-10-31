#!/usr/bin/env python3
"""Ask /health through the real Caddy path, standard library only. On the server, as root.

    caddy_probe.py --out FILE --cert FILE --key FILE [--token-file /etc/cricket/secrets/origin_auth_token] [--host api.example.org] [--port 443] [--interval 0.25] [--stop-file FILE]

Caddy only answers a TLS client that presents a certificate signed by the origin CA and sends the secret header Cloudflare adds. This probe presents the certificate and key it is given (a short-lived one made for
the flood check, never the one Cloudflare holds) and reads the header's value from the server's own secret file; the value is held in memory and never printed or written. Each probe is a new connection,
because a kept-alive connection would hide a connection ceiling. One CSV line per probe: time (UTC, to the millisecond), status or the kind of failure, milliseconds.
"""
import argparse
import http.client
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone


def probe(host: str, port: int, ctx: ssl.SSLContext, token: str, timeout: float) -> str:
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout)
        tls = ctx.wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        conn.sock = tls
        conn.request("GET", "/health", headers={"Host": host, "X-Origin-Auth": token})
        resp = conn.getresponse()
        resp.read()
        status = str(resp.status)
        conn.close()
        return status
    except Exception as error:  # noqa: BLE001 (every failure is a result, never skipped)
        return type(error).__name__
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--token-file", default="/etc/cricket/secrets/origin_auth_token")
    ap.add_argument("--host", default="api.example.org")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--stop-file")
    a = ap.parse_args(argv)
    token = open(a.token_file).read().strip()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE                       # a probe of 127.0.0.1 by name: what is measured is availability, and the server's identity is the host's own
    ctx.load_cert_chain(a.cert, a.key)
    new = not os.path.exists(a.out)
    with open(a.out, "a", buffering=1) as f:
        if new:
            f.write("time_utc,status,ms\n")
        while not (a.stop_file and os.path.exists(a.stop_file)):
            began = time.perf_counter()
            status = probe(a.host, a.port, ctx, token, a.timeout)
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z,{status},{(time.perf_counter() - began) * 1000:.1f}\n")
            time.sleep(max(0.0, a.interval - (time.perf_counter() - began)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
