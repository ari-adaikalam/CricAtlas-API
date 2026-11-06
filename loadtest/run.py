"""Local load test of the API, corrected after review. One server (started with the supported launcher, so one worker) and closed-loop clients in several processes.

    git worktree add /tmp/baseline <commit>                           # an older version to compare against, if wanted
    PYTHONPATH=api .venv/bin/python api/loadtest/run.py --label admission --out data/loadtest/admission.json
    PYTHONPATH=api .venv/bin/python api/loadtest/run.py --label before --code /tmp/baseline/api --out data/loadtest/before.json
    .venv/bin/python api/loadtest/run.py --compare data/loadtest/before.json data/loadtest/admission.json
    ... --quick    shorter runs to check the harness itself

What each part does, and what it does not:

  * Settings are controlled. Inherited CRICKET_API_* and UVICORN_* variables are removed, the benchmark settings are listed in the report, and the effective settings the server really reads are
    printed from the same environment and stored. A busy port stops the run. The server is always stopped, and every wait for a client process is bounded.
  * Each client has its own address (X-Forwarded-For through a listed proxy, 127.0.0.1), so the per client limits of the API act as they would for separate callers. Without that, every client
    would be one caller and the one heavy request per client rule would be all that a flood met.
  * Results are kept by outcome: successes (200, and 304 for conditional runs), 429, 503, other 5xx, timeouts and connection errors, each with its own count, share and latency. Throughput is reported
    twice: the successful rate, and the rate of all attempts. A refusal is never mixed into the latency of successes.
  * Conditional runs are given the ETags fetched beforehand, so every request is conditional from the first. A 200 in such a run is counted and shown as one.
  * Memory is the resident size of the server process, sampled every half second. The startup figure is taken before any route request (only /health has been asked).
  * Clients are polite or not. A polite client does what the documentation tells a client to do: after a 429 or a 503 it waits Retry-After seconds (1 s if there is none, never more than 10 s)
    with a little jitter before it asks again. A client that does not back off asks again at once, which for a refusal that costs the server almost nothing means thousands of attempts a second. The
    mixed traffic, the ladder and the probes use polite clients, because they stand for ordinary callers. The heavy flood and the overload use clients that never back off, because they stand for an
    attacker, and one mixed run is repeated without back-off to show the difference.
  * Recovery after an overload is the timestamps of probes taken every half second from three separate threads, not one probe.
The client and the server share one machine, so the client takes CPU from the server.
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import platform
import queue as queue_module
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
CLIENT_PROCESSES = 4
BENCH = {"CRICKET_API_RATE": "100000000", "CRICKET_API_BURST": "100000000", "CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "127.0.0.1"}


def address(index: int) -> str:
    return f"10.{(index >> 16) & 255}.{(index >> 8) & 255}.{index & 255}"


def retry_after_seconds(value) -> float:
    """What a polite client waits: Retry-After seconds when it is a number, 1 second when there is none (a server-level 503 has none), and never more than 10 seconds."""
    try:
        return min(10.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def client_address(spec: dict, worker_index: int, task_index: int) -> str:
    """The address one virtual client sends as X-Forwarded-For: its own, or the one shared by the whole group when the scenario is about one address."""
    return address(spec["id_offset"] if spec["same_address"] else spec["id_offset"] + worker_index * 1000 + task_index)


# The server -------------------------------------------------------------------------------------------------------------------

class Server:
    def __init__(self, code: Path, data: Path, env: dict):
        self.code, self.data, self.extra = code, data, env
        self.proc = None
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CRICKET_API_", "UVICORN_"))}
        self.env.update({"CRICKET_API_DATA": str(data), "CRICKET_API_SECRET": "loadtest-secret-0123456789abcdef", "PYTHONPATH": str(code), **env})

    def effective_settings(self) -> dict:
        """What the server will really read, printed by the same code in the same environment (the secret is left out)."""
        out = subprocess.run([sys.executable, "-c", "import dataclasses, json; from cricket_api.settings import Settings; d = dataclasses.asdict(Settings()); d.pop('cursor_secret', None); "
                              "print(json.dumps({k: str(v) for k, v in d.items()}))"], env=self.env, cwd=str(ROOT), capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout) if out.returncode == 0 else {"error": out.stderr[-300:]}

    def start(self) -> float:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", PORT)) == 0:
                raise SystemExit(f"port {PORT} is already in use, so the run was not started")
        self.proc = subprocess.Popen([sys.executable, "-m", "cricket_api", "--port", str(PORT), "--no-access-log"], env=self.env, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = time.time()
        while True:
            if self.proc.poll() is not None:
                raise SystemExit("the server stopped while starting")
            try:
                if httpx.get(BASE + "/health", timeout=1).status_code == 200:
                    return time.time() - started
            except httpx.HTTPError:
                pass
            if time.time() - started > 120:
                self.stop()
                raise SystemExit("the server did not start in 120 s")
            time.sleep(0.2)

    def rss_mb(self) -> float:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.proc.pid)], capture_output=True, text=True).stdout.strip()
        return int(out) / 1024 if out else 0.0

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)


class Memory(threading.Thread):
    def __init__(self, server: Server):
        super().__init__(daemon=True)
        self.server, self.peak, self.last, self.stop = server, 0.0, 0.0, threading.Event()

    def run(self):
        while not self.stop.is_set():
            self.last = self.server.rss_mb()
            self.peak = max(self.peak, self.last)
            self.stop.wait(0.5)


# The clients ------------------------------------------------------------------------------------------------------------------

async def client_loop(spec, worker_index, results, times):
    paths, weights = spec["paths"], spec["weights"]
    etags = spec["etags"] if spec["conditional"] else {}
    end = time.perf_counter() + spec["seconds"]
    n = spec["concurrency_per_process"]
    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    async with httpx.AsyncClient(base_url=BASE, limits=limits, timeout=spec["timeout"]) as http:
        async def loop(task_index):
            who = client_address(spec, worker_index, task_index)
            rng = random.Random(worker_index * 1000 + task_index)
            last_headers = {}
            while time.perf_counter() < end:
                path = rng.choices(paths, weights)[0] if weights else paths[0]
                headers = {"X-Forwarded-For": who}
                if path in etags:
                    headers["If-None-Match"] = etags[path]
                route = spec["labels"].get(path, "other")
                times["last_issue"] = time.time()
                began = time.perf_counter()
                try:
                    response = await http.get(path, headers=headers)
                    last_headers = response.headers
                    results.append((response.status_code, (time.perf_counter() - began) * 1000, route))
                except httpx.TimeoutException:
                    results.append(("timeout", (time.perf_counter() - began) * 1000, route))
                except httpx.HTTPError as error:
                    results.append((type(error).__name__, (time.perf_counter() - began) * 1000, route))
                times["last_answer"] = time.time()
                if spec["polite"] and results and results[-1][0] in (429, 503):
                    wait = retry_after_seconds(last_headers.get("retry-after"))
                    await asyncio.sleep(wait * (1 + 0.25 * rng.random()))
                if spec["think_ms"]:
                    await asyncio.sleep(spec["think_ms"] / 1000)
        await asyncio.gather(*[loop(i) for i in range(n)])


def client_process(spec, worker_index, out):
    results, times = [], {"last_issue": None, "last_answer": None}
    try:
        asyncio.run(client_loop(spec, worker_index, results, times))
    finally:
        out.put((spec["name"], results, times))


def percentile(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q / 100 * len(values)))], 2)


def outcome_name(status) -> str:
    if status in (200, 304):
        return str(status)
    if isinstance(status, int):
        return str(status) if status in (429, 503) else ("other_5xx" if status >= 500 else f"other_{status}")
    return "timeout" if status == "timeout" else "error"


# The admission class of each route the harness uses. The class table is the API's own (admission.TABLE), this only names the harness's routes.
CLASS_OF_ROUTE = {"leaderboard": "heavy", "scorecard": "medium", "career": "medium", "splits": "medium", "match": "light", "matches": "light", "search": "light", "meta": "none", "other": "other"}


def outcomes_of(rows):
    total = len(rows)
    groups = {}
    for status, ms, _ in rows:
        groups.setdefault(outcome_name(status), []).append(ms)
    outcomes = {k: {"count": len(v), "percent": round(100 * len(v) / total, 2) if total else 0, "p50_ms": percentile(v, 50), "p95_ms": percentile(v, 95), "p99_ms": percentile(v, 99)}
                for k, v in sorted(groups.items())}
    successes = len(groups.get("200", [])) + len(groups.get("304", []))
    return total, successes, outcomes


def summarise(name, results, seconds, concurrency, memory):
    total, successes, outcomes = outcomes_of(results)
    by_route, by_class = {}, {}
    for route in sorted({r[2] for r in results}):
        rows = [r for r in results if r[2] == route]
        n, ok, out = outcomes_of(rows)
        by_route[route] = {"class": CLASS_OF_ROUTE.get(route, "other"), "attempts": n, "success_percent": round(100 * ok / n, 2), "outcomes": out}
    for kind in sorted({CLASS_OF_ROUTE.get(r[2], "other") for r in results}):
        rows = [r for r in results if CLASS_OF_ROUTE.get(r[2], "other") == kind]
        n, ok, out = outcomes_of(rows)
        by_class[kind] = {"attempts": n, "success_percent": round(100 * ok / n, 2), "outcomes": out}
    return {"label": name, "concurrency": concurrency, "seconds": round(seconds, 1), "attempts": total, "attempts_per_s": round(total / seconds, 1), "successes": successes,
            "successful_per_s": round(successes / seconds, 1), "outcomes": outcomes, "by_class": by_class, "by_route": by_route,
            "peak_rss_mb": round(memory.peak, 1) if memory else None, "end_rss_mb": round(memory.last, 1) if memory else None}


def run_load(server, specs, seconds, label):
    """Run one or several client groups at the same time. Returns a summary for each group, by name."""
    memory = Memory(server)
    memory.start()
    out = mp.Queue()
    children, expected = [], 0
    for spec in specs:
        spec = {**spec, "seconds": seconds}
        processes = min(CLIENT_PROCESSES, spec["concurrency"])
        spec["concurrency_per_process"] = max(1, spec["concurrency"] // processes)
        for i in range(processes):
            child = mp.Process(target=client_process, args=({**spec}, i, out))
            child.start()
            children.append(child)
            expected += 1
    started = time.perf_counter()
    collected = {spec["name"]: [] for spec in specs}
    issued, answered = [], []
    deadline = started + seconds + 90
    for _ in range(expected):
        try:
            name, results, times = out.get(timeout=max(1, deadline - time.perf_counter()))
            collected[name] += results
            issued.append(times["last_issue"])
            answered.append(times["last_answer"])
        except queue_module.Empty:
            break
    for child in children:
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
    memory.stop.set()
    elapsed = time.perf_counter() - started
    rows = {}
    for spec in specs:
        row = summarise(f"{label}: {spec['name']}", collected[spec["name"]], elapsed, spec["concurrency"], memory)
        rows[spec["name"]] = row
        shown = ", ".join(f"{k} {v['count']:,} ({v['percent']}%, p50 {v['p50_ms']} ms)" for k, v in row["outcomes"].items())
        print(f"{row['label']:58} c={spec['concurrency']:<4} attempts {row['attempts_per_s']:>8}/s  successful {row['successful_per_s']:>8}/s  [{shown}]", flush=True)
        if len(row["by_class"]) > 1:
            print("      by class, share of successes: " + ", ".join(f"{k} {v['success_percent']}% of {v['attempts']:,}" for k, v in row["by_class"].items()), flush=True)
    rows["_times"] = {"clients_stopped_issuing_wall": max((t for t in issued if t), default=None), "all_requests_answered_wall": max((t for t in answered if t), default=None)}
    return rows


def spec(name, paths, concurrency, weights=None, conditional=False, etags=None, timeout=30, think_ms=0, id_offset=0, same_address=False, labels=None, polite=True):
    return {"name": name, "paths": paths, "weights": weights, "concurrency": concurrency, "conditional": conditional, "etags": etags or {}, "timeout": timeout, "think_ms": think_ms,
            "id_offset": id_offset, "same_address": same_address, "labels": labels or {}, "polite": polite}


def fetch_etags(paths):
    etags = {}
    with httpx.Client(base_url=BASE, timeout=60) as c:
        for path in paths:
            r = c.get(path, headers={"X-Forwarded-For": address(9_999_999)})
            if "etag" in r.headers:
                etags[path] = r.headers["etag"]
    return etags


# Recovery ---------------------------------------------------------------------------------------------------------------------

class Prober:
    """Probes every half second from three threads (/health, a light route, a medium route), started BEFORE the load so that the draining after it is seen, each sample with a wall clock time.
    The report measures three intervals, all from wall clock times:
      stopped_issuing -> all_answered   how long the clients themselves waited for their last outstanding requests (at most the client timeout)
      stopped_issuing -> recovered      from the moment the clients sent their last request until every probe has been fast (under 200 ms, status 200) three times in a row
      all_answered -> recovered         the part of that after the clients had given up on, or been answered for, everything they sent
    "Recovered" is judged only on probes sent after the clients stopped issuing requests."""
    def __init__(self, light, medium):
        self.paths = (("health", "/health", 8_000_001), ("light", light, 8_000_002), ("medium", medium, 8_000_003))
        self.samples, self.stop = [], threading.Event()
        self.threads = [threading.Thread(target=self.run, args=a, daemon=True) for a in self.paths]

    def run(self, name, path, who):
        with httpx.Client(base_url=BASE, timeout=120) as client:
            while not self.stop.is_set():
                t = time.time()
                began = time.perf_counter()
                try:
                    status = client.get(path, headers={"X-Forwarded-For": address(who)}).status_code
                except Exception as error:
                    status = type(error).__name__
                self.samples.append({"probe": name, "sent_wall": t, "status": status, "ms": round((time.perf_counter() - began) * 1000, 1)})
                self.stop.wait(0.5)

    def start(self):
        [t.start() for t in self.threads]

    def recovered_at(self, stopped_issuing):
        """Wall clock time at which every probe has answered fast three times running, counting only probes sent after the clients stopped issuing requests. For each probe it is the time its
        third fast answer came back, and the latest of the three probes is the answer. None if some probe has not had three fast answers in a row."""
        latest = 0.0
        for name, _, _ in self.paths:
            after = sorted((s for s in self.samples if s["probe"] == name and s["sent_wall"] >= stopped_issuing), key=lambda s: s["sent_wall"])
            fast = [s["status"] == 200 and s["ms"] < 200 for s in after]
            found = next((i for i in range(len(after) - 2) if fast[i] and fast[i + 1] and fast[i + 2]), None)
            if found is None:
                return None
            latest = max(latest, after[found + 2]["sent_wall"] + after[found + 2]["ms"] / 1000)
        return latest

    def wait_recovered(self, stopped_issuing, limit_seconds):
        deadline = time.time() + limit_seconds
        while time.time() < deadline:
            time.sleep(0.5)
            found = self.recovered_at(stopped_issuing)
            if found is not None:
                return found
        return None

    def finish(self):
        self.stop.set()
        [t.join(10) for t in self.threads]
        return sorted(self.samples, key=lambda s: s["sent_wall"])


# The run ----------------------------------------------------------------------------------------------------------------------

def git_state(code: Path) -> dict:
    def git(*args):
        return subprocess.run(["git", "-C", str(code), *args], capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "--short", "HEAD"), "uncommitted_changes": bool(git("status", "--porcelain", "--", ".")), "path": str(code)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--label", default="run")
    parser.add_argument("--data", default=str(ROOT / "data" / "api"))
    parser.add_argument("--code", default=str(ROOT / "api"), help="the api folder to run (an older checkout can be given)")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--compare", nargs=2)
    args = parser.parse_args()
    if args.compare:
        return compare(*args.compare)
    if not args.out:
        parser.error("--out is required")
    scale = 0.2 if args.quick else 1
    secs = lambda n: max(3, round(n * scale))
    code, data = Path(args.code).resolve(), Path(args.data).resolve()
    meta = json.loads((data / "meta.json").read_text())
    server = Server(code, data, BENCH)
    report = {"label": args.label, "environment": {"machine": platform.machine(), "cpu": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip(),
                                                  "logical_cpus": os.cpu_count(), "os": platform.platform(), "python": platform.python_version(), "client_processes": CLIENT_PROCESSES, "quick": args.quick},
              "code": git_state(code), "dataset": {"data_version": meta["data_version"], "built_at_utc": meta["built_at_utc"], "matches": meta["tables"]["matches"]["rows"], "balls": meta["tables"]["balls"]["rows"]},
              "benchmark_environment": BENCH, "effective_settings": server.effective_settings(), "runs": {}}
    try:
        report["server_start_seconds"] = round(server.start(), 1)
        report["startup_rss_mb_before_any_route_request"] = round(server.rss_mb(), 1)
        print(f"== {args.label}: code {report['code']}, data {meta['data_version']}, started in {report['server_start_seconds']} s, {report['startup_rss_mb_before_any_route_request']} MB before any route request")
        with httpx.Client(base_url=BASE, timeout=60) as c:
            match = c.get("/v1/matches", params={"limit": 1}).json()["data"][0]["match_id"]
            player = c.get("/v1/players", params={"q": "BA Stokes"}).json()["data"][0]["player_id"]
        routes = {"leaderboard": "/v1/leaderboards/batting?metric=average", "match": f"/v1/matches/{match}", "matches": "/v1/matches?team=England&limit=50", "search": "/v1/matches?q=england%20australia%202019",
                  "scorecard": f"/v1/matches/{match}/scorecard", "career": f"/v1/players/{player}/batting", "splits": f"/v1/players/{player}/batting/splits?by=venue", "meta": "/v1/meta"}
        labels = {path: name for name, path in routes.items()}
        mixed = [(routes["meta"], 5), (routes["match"], 20), (routes["matches"], 20), (routes["search"], 10), (routes["scorecard"], 10), (routes["career"], 15), (routes["splits"], 5), (routes["leaderboard"], 5)]
        mpaths, mweights = [p for p, _ in mixed], [w for _, w in mixed]
        report["mixed_traffic_weights"] = dict(mixed)

        print("== mixed traffic (polite clients)")
        report["runs"]["mixed"] = run_load(server, [spec("mixed", mpaths, 16, mweights, labels=labels)], secs(30), "mixed 30 s")["mixed"]
        report["runs"]["mixed_no_backoff"] = run_load(server, [spec("mixed", mpaths, 16, mweights, id_offset=50_000, labels=labels, polite=False)], secs(30), "mixed 30 s, clients that do not back off")["mixed"]
        print("== increasing concurrency, mixed traffic (polite clients)")
        report["runs"]["ladder"] = [run_load(server, [spec("mixed", mpaths, c, mweights, id_offset=c * 100, labels=labels)], secs(10), f"ladder c={c}")["mixed"] for c in (1, 4, 16, 64, 256)]

        print("== conditional requests (every request carries the ETag fetched beforehand)")
        report["runs"]["conditional"] = {}
        for name in ("match", "scorecard", "matches", "leaderboard"):
            etags = fetch_etags([routes[name]])
            report["runs"]["conditional"][name] = run_load(server, [spec("conditional", [routes[name]], 8, conditional=True, etags=etags, labels=labels)], secs(10), f"conditional {name}")["conditional"]

        print("== light routes under a heavy flood: 64 clients with their own addresses on the leaderboard, and probes on two light routes")
        quiet = run_load(server, [spec("light_probe", [routes["match"], routes["matches"]], 2, think_ms=20, id_offset=7_000_000, labels=labels)], secs(10), "light probes, quiet")["light_probe"]
        flood = run_load(server, [spec("heavy_flood", [routes["leaderboard"]], 64, id_offset=100_000, labels=labels, polite=False), spec("light_probe", [routes["match"], routes["matches"]], 2, think_ms=20, id_offset=7_000_000, labels=labels)],
                         secs(20), "heavy flood + light probes")
        report["runs"]["light_under_flood"] = {"quiet": quiet, "flood": flood["heavy_flood"], "probes_during_flood": flood["light_probe"]}

        print("== overload and recovery: 512 clients with their own addresses on the leaderboard, 10 s client timeout, then the clients stop sending")
        memory_before = server.rss_mb()
        prober = Prober(routes["match"], routes["scorecard"])
        prober.start()
        time.sleep(2)
        rows = run_load(server, [spec("overload", [routes["leaderboard"]], 512, timeout=10, id_offset=200_000, labels=labels, polite=False)], secs(20), "overload 512")
        over, times = rows["overload"], rows["_times"]
        recovered_at = prober.wait_recovered(times["clients_stopped_issuing_wall"], 150)
        samples = prober.finish()
        stopped, answered = times["clients_stopped_issuing_wall"], times["all_requests_answered_wall"]
        intervals = {"stopped_issuing_to_all_answered_s": round(answered - stopped, 2), "stopped_issuing_to_recovered_s": round(recovered_at - stopped, 2) if recovered_at else None,
                     "all_answered_to_recovered_s": round(recovered_at - answered, 2) if recovered_at else None}
        print(f"   intervals (wall clock): {intervals} (None means not recovered within 150 s)")
        after_stop = [s for s in samples if s["sent_wall"] >= stopped]
        print(f"   worst probe sent after the clients stopped: {max((s['ms'] for s in after_stop), default=0):.0f} ms; worst during the load: {max((s['ms'] for s in samples if s['sent_wall'] < stopped), default=0):.0f} ms")
        for s_ in samples:
            s_["sent_after_clients_stopped_s"] = round(s_.pop("sent_wall") - stopped, 2)
        report["runs"]["overload"] = {"load": over, "intervals": intervals, "probes": samples, "rss_mb_before": round(memory_before, 1), "rss_mb_after_recovery": round(server.rss_mb(), 1)}
        after = run_load(server, [spec("mixed", mpaths, 16, mweights, id_offset=300_000, labels=labels)], secs(10), "mixed after overload")["mixed"]
        report["runs"]["after_overload"] = after
        report["end_rss_mb"] = round(server.rss_mb(), 1)
    finally:
        server.stop()

    print("== normal settings, rate limit on (a separate server)")
    normal = Server(code, data, {"CRICKET_API_CLIENT_IP": "proxy", "CRICKET_API_TRUSTED_PROXIES": "127.0.0.1"})
    try:
        normal.start()
        report["runs"]["rate_limited"] = run_load(normal, [spec("one_address", [routes["meta"]], 8, id_offset=500_000, same_address=True, labels=labels)], secs(10), "default limits, 8 clients one address")["one_address"]
        report["effective_settings_rate_limited"] = normal.effective_settings()
    finally:
        normal.stop()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1))
    print("written", args.out)


def compare(before_path, after_path):
    a, b = (json.loads(Path(p).read_text()) for p in (before_path, after_path))

    def cell(run, outcome=None, key="successful_per_s"):
        if outcome:
            return (run["outcomes"].get(outcome) or {}).get(key)
        return run.get(key)

    def classes(run):
        return " / ".join(f"{run['by_class'][k]['success_percent']}%" if k in run["by_class"] else "-" for k in ("light", "medium", "heavy"))

    def refused(run):
        return f"{cell(run, '429', 'percent') or 0}% 429, {cell(run, '503', 'percent') or 0}% 503, {cell(run, 'timeout', 'percent') or 0}% timeout, {cell(run, 'error', 'percent') or 0}% error"
    print(f"| Measure | {a['label']} ({a['code']['commit']}) | {b['label']} ({b['code']['commit']}) |\n|---|---|---|")
    rows = [("Startup memory before any route request, MB", a["startup_rss_mb_before_any_route_request"], b["startup_rss_mb_before_any_route_request"]),
            ("Mixed: successful requests per second", cell(a["runs"]["mixed"]), cell(b["runs"]["mixed"])),
            ("Mixed: share of successes by class, light / medium / heavy", classes(a["runs"]["mixed"]), classes(b["runs"]["mixed"])),
            ("Mixed: p50 / p99 ms of successes", f"{cell(a['runs']['mixed'], '200', 'p50_ms')} / {cell(a['runs']['mixed'], '200', 'p99_ms')}", f"{cell(b['runs']['mixed'], '200', 'p50_ms')} / {cell(b['runs']['mixed'], '200', 'p99_ms')}"),
            ("Mixed: refusals and failures", refused(a["runs"]["mixed"]), refused(b["runs"]["mixed"])),
            ("Mixed: peak memory MB", cell(a["runs"]["mixed"], key="peak_rss_mb"), cell(b["runs"]["mixed"], key="peak_rss_mb"))]
    rows.append(("Mixed, clients that do NOT back off: successful per second; share of successes by class", f"{cell(a['runs']['mixed_no_backoff'])}; {classes(a['runs']['mixed_no_backoff'])}",
                 f"{cell(b['runs']['mixed_no_backoff'])}; {classes(b['runs']['mixed_no_backoff'])}"))
    rows.append(("Mixed, clients that do NOT back off: refusals and failures", refused(a["runs"]["mixed_no_backoff"]), refused(b["runs"]["mixed_no_backoff"])))
    for i, c in enumerate((1, 4, 16, 64, 256)):
        ra, rb = a["runs"]["ladder"][i], b["runs"]["ladder"][i]
        rows.append((f"Ladder c={c}: successful per second; share of successes by class (light / medium / heavy)", f"{ra['successful_per_s']}; {classes(ra)}", f"{rb['successful_per_s']}; {classes(rb)}"))
        rows.append((f"Ladder c={c}: refusals and failures", refused(ra), refused(rb)))
    for name in ("match", "scorecard", "matches", "leaderboard"):
        ra, rb = a["runs"]["conditional"][name], b["runs"]["conditional"][name]
        rows.append((f"Conditional {name}: 304 per second; share of full 200 responses; refusals", f"{round((cell(ra, '304', 'count') or 0) / ra['seconds'], 1)}; {cell(ra, '200', 'percent') or 0}%; {refused(ra)}",
                     f"{round((cell(rb, '304', 'count') or 0) / rb['seconds'], 1)}; {cell(rb, '200', 'percent') or 0}%; {refused(rb)}"))
    for label, key in (("quiet", "quiet"), ("during the heavy flood", "probes_during_flood")):
        ra, rb = a["runs"]["light_under_flood"][key], b["runs"]["light_under_flood"][key]
        rows.append((f"Light probes {label}: share of successes; p50 / p99 ms of successes; 503 share", f"{ra['by_class']['light']['success_percent']}%; {cell(ra, '200', 'p50_ms')} / {cell(ra, '200', 'p99_ms')}; {cell(ra, '503', 'percent') or 0}%",
                     f"{rb['by_class']['light']['success_percent']}%; {cell(rb, '200', 'p50_ms')} / {cell(rb, '200', 'p99_ms')}; {cell(rb, '503', 'percent') or 0}%"))
    fa, fb = a["runs"]["light_under_flood"]["flood"], b["runs"]["light_under_flood"]["flood"]
    rows.append(("Heavy flood: successful per second; p50 ms of successes; refusals", f"{fa['successful_per_s']}; {cell(fa, '200', 'p50_ms')}; {refused(fa)}", f"{fb['successful_per_s']}; {cell(fb, '200', 'p50_ms')}; {refused(fb)}"))
    oa, ob = a["runs"]["overload"], b["runs"]["overload"]
    for key, text in (("stopped_issuing_to_all_answered_s", "Overload: clients stopped sending -> their last request answered, s"), ("stopped_issuing_to_recovered_s", "Overload: clients stopped sending -> all three probes fast again, s"),
                      ("all_answered_to_recovered_s", "Overload: last client request answered -> all three probes fast again, s")):
        rows.append((text, oa["intervals"][key], ob["intervals"][key]))
    rows.append(("Overload: successful per second; outcomes", f"{oa['load']['successful_per_s']}; {refused(oa['load'])}", f"{ob['load']['successful_per_s']}; {refused(ob['load'])}"))
    rows.append(("Overload: peak memory MB; memory after recovery MB", f"{oa['load']['peak_rss_mb']}; {oa['rss_mb_after_recovery']}", f"{ob['load']['peak_rss_mb']}; {ob['rss_mb_after_recovery']}"))
    rows.append(("Mixed after the overload: successful per second; share by class", f"{cell(a['runs']['after_overload'])}; {classes(a['runs']['after_overload'])}", f"{cell(b['runs']['after_overload'])}; {classes(b['runs']['after_overload'])}"))
    ra, rb = a["runs"]["rate_limited"], b["runs"]["rate_limited"]
    rows.append(("Default limits, 8 clients sharing ONE address: share of 200 and 429; p50 ms of 429s", f"{cell(ra, '200', 'percent') or 0}%, {cell(ra, '429', 'percent') or 0}%; {cell(ra, '429', 'p50_ms')}", f"{cell(rb, '200', 'percent') or 0}%, {cell(rb, '429', 'percent') or 0}%; {cell(rb, '429', 'p50_ms')}"))
    for name, x, y in rows:
        print(f"| {name} | {x} | {y} |")


if __name__ == "__main__":
    mp.set_start_method("spawn")
    main()
