import http.client, json, os, socket, sys, time, hashlib
class U(http.client.HTTPConnection):
    def __init__(self, timeout): super().__init__("localhost", timeout=timeout)
    def connect(self): self.sock = socket.socket(socket.AF_UNIX); self.sock.settimeout(self.timeout); self.sock.connect(os.environ["API_SOCKET"])
path = sys.argv[1]; who = sys.argv[2] if len(sys.argv) > 2 else "10.9.0.1"; timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 150
t = time.perf_counter()
try:
    c = U(timeout); c.request("GET", path, headers={"Host": "x.invalid", "X-Client-IP": who}); r = c.getresponse(); body = r.read()
    out = {"status": r.status, "secs": round(time.perf_counter() - t, 2), "etag": r.getheader("ETag"), "bytes": len(body), "sha": hashlib.sha256(body).hexdigest()[:16], "request_id": r.getheader("X-Request-ID"), "ctype": r.getheader("Content-Type")}
    if r.status >= 400: out["body"] = body.decode("utf-8", "replace")[:300]
except Exception as e:
    out = {"status": 0, "error": type(e).__name__, "secs": round(time.perf_counter() - t, 2)}
print(json.dumps(out))
