#!/usr/local/bin/python
"""Container health: GET /health over the unix socket, the same way the proxy reaches the API. Exit 0 when it answers 200 with status ok, 1 otherwise. No network, no output beyond the exit code."""
import http.client
import json
import os
import socket
import sys


class Unix(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=4)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(4)
        self.sock.connect(self._path)


try:
    connection = Unix(os.environ["API_SOCKET"])
    connection.request("GET", "/health", headers={"Host": "health.invalid", "X-Client-IP": "127.0.0.1"})
    response = connection.getresponse()
    body = json.loads(response.read(4096))
    sys.exit(0 if response.status == 200 and body.get("status") == "ok" else 1)
except Exception:
    sys.exit(1)
