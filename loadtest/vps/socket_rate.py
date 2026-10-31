"""Open-loop load against the API's unix socket at a fixed OFFERED rate, standard library only (it runs in a small container with no HTTP client library).

    python socket_rate.py '[{"name": "r8", "rate": 8, "seconds": 90, "mix": "mixed", "clients": 400}, ...]'

socket_load.py is closed-loop: each caller waits for its answer, so the rate it offers falls as the server slows, and a slow generator and a slow server look the same. This one decides WHEN each request is
sent before the phase starts (a seeded Poisson schedule, so a rerun offers the same arrivals) and sends it at that time whatever happened to earlier requests. What the server cannot keep up with shows up as
latency, refusals and timeouts, never as a lower offered rate.

Each phase records, apart from the outcomes:
  lateness   how long after its planned time each request actually started. If it grows, the GENERATOR is the bottleneck (all its worker threads were busy, or its CPU was), not the API. The phase says so.
  generator_cpu_s / wall   the generator's own CPU use, so contention on its core is visible
  health     a separate prober asks /health every 0.25 s THROUGHOUT the phase, from its own connection: how many answered 200, every other outcome counted by kind (a timeout is a failure, never skipped), p50, p99, max.
             A prober that could not run is reported as such and the phase is not valid. `n` per class is the sample count behind every percentile and share.
  pooled latency percentiles of the successes (kept per phase from every request, so they are real percentiles, unlike averaging the percentiles of sub-phases)
Outcomes are kept apart and never mixed: 200, 429 (the per-client rate limit), 503_admission (a problem document, carries Retry-After), 503_plain (the server's own connection limit: no Retry-After),
timeout, error:NAME and other_STATUS. Requests come from `clients` distinct addresses in turn (X-Client-IP, which the API trusts on its socket the way it trusts the proxy), so the per-client limit does not decide
the result unless `clients` is set too small, and the phase reports the offered rate per client so that is visible. Parameters vary from request to request, but some routes have few variants (the competitions list has one), so each class reports
its `distinct_path_share`: a low share means repeated paths over the same data, which run against warm engine buffers and a warm page cache and are cheaper than a first-time request. The API itself keeps no result cache (CRICKET_API_SHARED_CACHE only adds headers for a CDN), so nothing is skipped, but a result on repeated paths must not be read as the cost of first-time work. The `diverse` mix asks for many different players, matches and parameters. That makes requests DIVERSE, which makes a fully warm result unlikely. It does NOT make the engine's buffers or the page cache cold (they stay warm from earlier requests, and a diverse request can still hit warm pages), so it is not a cold-cache measurement.
"""
import http.client
import json
import os
import queue
import random
import resource
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import socket_load as sl  # noqa: E402  (the same request mixes and paths as the closed-loop generator)

SOCK = os.environ.get("API_SOCKET", "/run/cricket/api.sock")
TIMEOUT = float(os.environ.get("RATE_TIMEOUT", "30"))


class Conn(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get("API_SOCKET", SOCK))


def schedule(rate: float, seconds: float, seed: str) -> list:
    """Planned send times, in seconds from the start of the phase: a Poisson process of the given rate, the same every run for the same seed."""
    rnd = random.Random("arrivals-" + seed)
    t, out = 0.0, []
    while True:
        t += rnd.expovariate(rate)
        if t >= seconds:
            return out
        out.append(t)


def classify(status, retry_after) -> str:
    if status == 200:
        return "200"
    if status == 429:
        return "429"
    if status == 503:
        return "503_admission" if retry_after else "503_plain"
    if isinstance(status, str):
        return status
    return f"other_{status}"


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


HARVEST = {"players": [], "matches": []}
PLAYER_ID = __import__("re").compile(r'"player_id"\s*:\s*"([0-9a-f]{8})"')
MATCH_ID = __import__("re").compile(r'"match_id"\s*:\s*(\d+)')


def harvest(per_query: int = 100) -> dict:
    """Real player and match ids, asked of the API itself, so the `diverse` mix can ask for a different player or match almost every time instead of the one the older mix used."""
    for q in ("a", "e", "i", "o", "u", "an", "ar", "er", "in", "on", "sh", "kh", "ma", "sa", "ra", "da"):
        try:
            c = Conn(TIMEOUT)
            c.request("GET", f"/v1/players?q={q}&limit={per_query}", headers={"Host": "load.invalid", "X-Client-IP": "10.99.0.1"})
            text = c.getresponse().read().decode("utf-8", "replace")
            c.close()
        except Exception:  # noqa: BLE001
            continue
        for found in PLAYER_ID.findall(text):
            if found not in HARVEST["players"]:
                HARVEST["players"].append(found)
    for team in sl.TEAMS + ["Sri Lanka", "West Indies", "Bangladesh", "Afghanistan"]:
        try:
            c = Conn(TIMEOUT)
            c.request("GET", f"/v1/matches?limit=100&q={team.replace(' ', '%20')}", headers={"Host": "load.invalid", "X-Client-IP": "10.99.0.1"})
            text = c.getresponse().read().decode("utf-8", "replace")
            c.close()
        except Exception:  # noqa: BLE001
            continue
        for found in MATCH_ID.findall(text):
            if int(found) not in HARVEST["matches"]:
                HARVEST["matches"].append(int(found))
    return HARVEST


def diverse_pick(r: random.Random):
    """The same 60/30/10 split as the `mixed` mix, with the medium and light requests spread over harvested players and matches."""
    x = r.uniform(0, 100)
    pid, mid = r.choice(HARVEST["players"]), r.choice(HARVEST["matches"])
    if x < 10:
        return "heavy", sl.heavy(r)
    if x < 40:
        return "medium", r.choice([f"/v1/players/{pid}/batting?season={r.choice(sl.SEASONS)}", f"/v1/players/{pid}/batting/seasons?format={r.choice(['TEST', 'ODI', 'T20'])}",
                                   f"/v1/matches/{mid}/scorecard", f"/v1/players/{pid}/matches?limit={r.randint(5, 100)}", f"/v1/players/{pid}/bowling/splits?by=opponent&format={r.choice(['TEST', 'ODI', 'T20'])}"])
    return "light", r.choice([f"/v1/matches?limit={r.randint(1, 100)}&q={sl.quote(r.choice(sl.TEAMS))}", f"/v1/players?q={r.choice('abcdefghijklmnopqrstuvwxyz')}{r.choice('aeiou')}{r.choice('nrstlmk')}&limit={r.randint(5, 60)}",
                              f"/v1/matches/{mid}", f"/v1/teams?q={sl.quote(r.choice(sl.TEAMS))}&gender={r.choice(['male', 'female'])}"])


def one(path: str, who: str):
    t = time.perf_counter()
    retry = None
    try:
        c = Conn(TIMEOUT)
        c.request("GET", path, headers={"Host": "load.invalid", "X-Client-IP": who})
        resp = c.getresponse()
        resp.read()
        status, retry = resp.status, resp.getheader("Retry-After")
        c.close()
    except socket.timeout:
        status = "timeout"
    except Exception as e:  # noqa: BLE001
        status = "error:" + type(e).__name__
    return status, retry, (time.perf_counter() - t) * 1000


def health_prober(stop: threading.Event, rows: list, lock: threading.Lock):
    """/health every 0.25 s until told to stop. Every attempt is recorded, a failure as what it was, so the result can never read healthier than the probes were."""
    while not stop.is_set():
        status, retry, ms = one("/health", "10.250.0.1")
        with lock:
            rows.append((classify(status, retry), ms))
        stop.wait(0.25)


def health_summary(rows: list, prober_alive: bool) -> dict:
    lat = [ms for out, ms in rows if out == "200"]
    outcomes = {}
    for out, _ in rows:
        outcomes[out] = outcomes.get(out, 0) + 1
    return {"n": len(rows), "ok": outcomes.get("200", 0), "outcomes": outcomes, "p50": pct(lat, .5), "p99": pct(lat, .99), "max": round(max(lat), 1) if lat else None, "prober_ran": bool(rows) and prober_alive}


def run_phase(phase: dict, index: int) -> dict:
    name, rate, seconds, mix = phase["name"], float(phase["rate"]), float(phase["seconds"]), phase.get("mix", "mixed")
    clients, workers = int(phase.get("clients", 400)), int(phase.get("workers", 128))
    times = schedule(rate, seconds, name)
    rnd = random.Random("requests-" + name)
    jobs = []
    if mix == "diverse" and not (HARVEST["players"] and HARVEST["matches"]):
        harvest()
        if not (HARVEST["players"] and HARVEST["matches"]):
            raise SystemExit("the diverse mix needs player and match ids, and the API gave none")
    for i, t in enumerate(times):
        family = None
        if mix == "final":
            cls, family, path = sl.pick_final(rnd)
        else:
            cls, path = diverse_pick(rnd) if mix == "diverse" else sl.pick(rnd, mix)
        jobs.append((t, cls, path, f"10.{100 + index % 100}.{(i % clients) // 250}.{(i % clients) % 250 + 1}", family))
    q: queue.Queue = queue.Queue()
    results, lock = [], threading.Lock()
    start_box = {}

    def worker():
        while True:
            item = q.get()
            if item is None:
                return
            planned, cls, path, who, family = item
            began = time.perf_counter() - start_box["t0"]
            status, retry, ms = one(path, who)
            with lock:
                results.append((cls, classify(status, retry), ms, began - planned, path, family))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    health_rows, health_lock, health_stop = [], threading.Lock(), threading.Event()
    prober = threading.Thread(target=health_prober, args=(health_stop, health_rows, health_lock), daemon=True)
    prober.start()
    cpu0 = resource.getrusage(resource.RUSAGE_SELF)
    start_box["t0"] = time.perf_counter()
    wall0 = time.time()
    for planned, cls, path, who, family in jobs:
        delay = planned - (time.perf_counter() - start_box["t0"])
        if delay > 0:
            time.sleep(delay)
        q.put((planned, cls, path, who, family))
    sent = len(jobs)
    deadline = time.perf_counter() + TIMEOUT + 5
    while time.perf_counter() < deadline:
        with lock:
            if len(results) >= sent:
                break
        time.sleep(0.05)
    for _ in threads:
        q.put(None)
    health_stop.set()
    prober.join(timeout=TIMEOUT + 5)
    elapsed = time.time() - wall0
    cpu1 = resource.getrusage(resource.RUSAGE_SELF)
    with lock:
        got = list(results)
    outcomes, by_class, lat, late = {}, {}, [], []
    fams = {}
    for cls, out, ms, lateness, path, family in got:
        outcomes[out] = outcomes.get(out, 0) + 1
        if family:
            f = fams.setdefault(family, {"outcomes": {}, "lat": []})
            f["outcomes"][out] = f["outcomes"].get(out, 0) + 1
            if out == "200":
                f["lat"].append(ms)
        c = by_class.setdefault(cls, {"outcomes": {}, "lat": [], "paths": []})
        c["paths"].append(path)
        c["outcomes"][out] = c["outcomes"].get(out, 0) + 1
        late.append(lateness * 1000)
        if out == "200":
            lat.append(ms)
            c["lat"].append(ms)
    cpu = (cpu1.ru_utime - cpu0.ru_utime) + (cpu1.ru_stime - cpu0.ru_stime)
    late_p99 = pct(late, 0.99) or 0
    return {"phase": name, "mix": mix, "offered_per_s": round(sent / seconds, 2), "planned_rate": rate, "clients": clients, "offered_per_client_per_min": round(60 * rate / clients, 2), "workers": workers,
            "seconds": round(elapsed, 1), "sent": sent, "answered": len(got), "unanswered": sent - len(got), "ok_per_s": round(outcomes.get("200", 0) / seconds, 2), "outcomes": outcomes,
            "success_share": round(outcomes.get("200", 0) / max(1, len(got)), 4),
            "ok_latency_ms": {"p50": pct(lat, .5), "p95": pct(lat, .95), "p99": pct(lat, .99), "max": round(max(lat), 1) if lat else None},
            "by_class": {k: {"n": sum(v["outcomes"].values()), "outcomes": v["outcomes"], "success_share": round(v["outcomes"].get("200", 0) / sum(v["outcomes"].values()), 4), "p50": pct(v["lat"], .5), "p95": pct(v["lat"], .95), "p99": pct(v["lat"], .99),
                             "distinct_path_share": round(len(set(v["paths"])) / len(v["paths"]), 3)} for k, v in by_class.items()},
            "by_family": {k: {"n": sum(v["outcomes"].values()), "outcomes": v["outcomes"], "p50": pct(v["lat"], .5), "p99": pct(v["lat"], .99)} for k, v in sorted(fams.items())},
            "lateness_ms": {"p50": pct(late, .5), "p99": pct(late, .99), "max": round(max(late), 1) if late else None},
            "health": health_summary(list(health_rows), True),
            "class_samples": {k: sum(v["outcomes"].values()) for k, v in by_class.items()},
            "generator_cpu_s": round(cpu, 2), "generator_cpu_of_one_core": round(cpu / max(elapsed, 1e-9), 3),
            "generator_limited": bool(late_p99 > 250 or cpu / max(elapsed, 1e-9) > 0.8)}


def main(argv: list) -> int:
    for n, phase in enumerate(json.loads(argv[1]), 1):
        print(json.dumps(run_phase(phase, n)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
