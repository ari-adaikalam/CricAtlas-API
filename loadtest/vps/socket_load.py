"""Closed-loop load against the API's unix socket, standard library only (it runs in a small container that has no HTTP client library).

    python load.py '[{"name": "...", "seconds": 45, "clients": 8, "mix": "mixed", "polite": true, "think_ms": 0}, ...]'

Every client has its own address (X-Client-IP, which the API trusts on its unix socket the way it trusts the proxy), so the per-client limits act as they would for separate callers. Parameters are varied from request to request
so that no answer can come from a cache of any kind. A polite client waits Retry-After (at most 5 s, 1 s if none, with jitter) after a 429 or 503. A client that is not polite asks again at once.
A separate probe asks /health every 0.25 s through every phase. Results are kept by outcome and never mixed: a refusal is not part of the latency of successes.
"""
import http.client
import json
import os
import random
import signal
import socket
import sys
import threading
import time
from urllib.parse import quote

SOCK = os.environ.get("API_SOCKET", "/run/cricket/api.sock")
PLAYER, MATCH = "e087956b", 1152848
SEASONS = [str(y) for y in range(2008, 2026)]
COLLECTIONS = ["tests", "odis", "t20s", "ipl", "bbl", "psl", "hnd", "cpl", "sat"]
TEAMS = ["England", "Australia", "India", "South Africa", "New Zealand", "Pakistan"]


class Conn(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCK)


def light(r):
    return r.choice([f"/v1/matches?limit={r.randint(10, 60)}&q={quote(r.choice(TEAMS))}", f"/v1/players?q={r.choice(['Stokes', 'Kohli', 'Smith', 'Root', 'Warner', 'Williamson'])}&limit={r.randint(5, 40)}",
                     "/v1/competitions", f"/v1/teams?q={quote(r.choice(TEAMS))}&gender=male", f"/v1/venues?q={r.choice(['Lord', 'Oval', 'Melbourne', 'Eden'])}"])


def medium(r):
    return r.choice([f"/v1/players/{PLAYER}/batting?season={r.choice(SEASONS)}", f"/v1/players/{PLAYER}/batting/seasons?format={r.choice(['TEST', 'ODI', 'T20'])}", f"/v1/matches/{MATCH}/scorecard",
                     f"/v1/players/{PLAYER}/matches?limit={r.randint(20, 100)}", f"/v1/players/{PLAYER}/bowling/splits?by=opponent&format={r.choice(['TEST', 'ODI', 'T20'])}"])


def heavy(r):
    which = r.choice(["batting", "bowling"])
    metric = r.choice(["runs", "average", "strike_rate", "hundreds", "fours"] if which == "batting" else ["wickets", "maidens", "average", "economy"])
    extra = r.choice(["", f"&season={r.choice(SEASONS)}", f"&collection={r.choice(COLLECTIONS)}", f"&format={r.choice(['TEST', 'ODI', 'T20'])}"])
    return f"/v1/leaderboards/{which}?metric={metric}&limit={r.randint(10, 100)}{extra}"


MIXES = {"mixed": [(light, 60), (medium, 30), (heavy, 10)], "soak": [(light, 70), (medium, 27), (heavy, 3)], "heavy": [(heavy, 100)], "light": [(light, 100)]}

# The final mix (the validation run and the Gate 1 comparison): every changed family, drawn from a workload file that is built from the data (build_workload.py) and mounted into the container, so that careers of
# every size, players with no career, Super Over matches and real batter and bowler pairs are asked for, and each family is counted on its own. Class shares are the soak's 70/27/3.
WORKLOAD_PATH = os.environ.get("WORKLOAD", "/workload.json")
FINAL_SHARES = (("light", 70), ("medium", 27), ("heavy", 3))
MEDIUM_FAMILIES = ("batting_career", "batting_seasons", "batting_splits", "bowling_career", "bowling_seasons", "bowling_splits", "fielding_career", "fielding_seasons", "fielding_splits",
                   "history", "scorecard", "matchup")
LIGHT_FAMILIES = ("matches_list", "player_search", "competitions", "teams", "venues", "match_detail", "ball_page")
HEAVY_FAMILIES = ("leaderboard_batting", "leaderboard_bowling")
ALL_FAMILIES = LIGHT_FAMILIES + MEDIUM_FAMILIES + HEAVY_FAMILIES
_WORKLOAD = {}


def workload() -> dict:
    if not _WORKLOAD:
        try:
            data = json.loads(open(WORKLOAD_PATH).read())
        except (OSError, ValueError) as error:
            raise SystemExit(f"the final mix needs the workload file {WORKLOAD_PATH}: {error}")
        for key in ("players", "matches", "pairs", "super_over_matches", "ball_targets"):
            if not data.get(key):
                raise SystemExit(f"the workload file has no {key}")
        _WORKLOAD.update(data)
    return _WORKLOAD


def pick_final(r, _mix=None):
    """(class, family, path) from the final mix. Each medium family is equally likely, with the player, match or pair drawn from the workload (about 3 percent of player requests go to a player with no career)."""
    w = workload()
    x = r.uniform(0, 100)
    if x < FINAL_SHARES[0][1]:
        family = r.choice(LIGHT_FAMILIES)
        mid = r.choice(w["matches"])["id"]
        ball_match, ball_innings = r.choice(w["ball_targets"])
        path = {"matches_list": f"/v1/matches?limit={r.randint(10, 60)}&q={quote(r.choice(TEAMS))}", "player_search": f"/v1/players?q={r.choice(['Stokes', 'Kohli', 'Smith', 'Root', 'Warner', 'Williamson', 'Bumrah', 'Rashid'])}&limit={r.randint(5, 40)}",
                "competitions": "/v1/competitions", "teams": f"/v1/teams?q={quote(r.choice(TEAMS))}&gender={r.choice(['male', 'female'])}", "venues": f"/v1/venues?q={r.choice(['Lord', 'Oval', 'Melbourne', 'Eden', 'Wankhede'])}",
                "match_detail": f"/v1/matches/{mid}", "ball_page": f"/v1/matches/{ball_match}/innings/{ball_innings}/balls?limit={r.choice([50, 100, 200])}"}[family]
        return "light", family, path
    if x < FINAL_SHARES[0][1] + FINAL_SHARES[1][1]:
        family = r.choice(MEDIUM_FAMILIES)
        player = r.choice(w["players"] if r.random() > 0.03 else w["no_career"] or w["players"])["id"]
        flt = r.choice(["", "", f"?format={r.choice(['TEST', 'ODI', 'T20'])}", f"?collection={r.choice(COLLECTIONS)}", f"?season={r.choice(SEASONS)}"])
        by = r.choice(["opponent", "venue", "competition", "format"])
        if family in ("scorecard",):
            match = r.choice(w["super_over_matches"] if r.random() < 0.15 else w["matches"])["id"]
            return "medium", family, f"/v1/matches/{match}/scorecard"
        if family == "matchup":
            batter, bowler = r.choice(w["pairs"])
            return "medium", family, f"/v1/matchups?batter={batter}&bowler={bowler}" + ("" if r.random() < 0.7 else f"&format={r.choice(['TEST', 'ODI', 'T20'])}")
        if family == "history":
            return "medium", family, f"/v1/players/{player}/matches?limit={r.randint(10, 100)}" + ("" if not flt else "&" + flt[1:])
        kind, what = family.split("_")
        if what == "seasons" and flt.startswith("?season="):
            flt = ""                                    # the seasons routes group by season and take no season filter (a 400, found by the final-mix gate on 2026-10-13)
        tail = {"career": "", "seasons": "/seasons", "splits": f"/splits?by={by}"}[what]
        if what == "splits":
            return "medium", family, f"/v1/players/{player}/{kind}{tail}" + ("" if not flt else "&" + flt[1:])
        return "medium", family, f"/v1/players/{player}/{kind}{tail}{flt}"
    kind = r.choice(["batting", "bowling"])
    metric = r.choice(["runs", "average", "strike_rate", "hundreds", "fours"] if kind == "batting" else ["wickets", "maidens", "average", "economy"])
    extra = r.choice(["", f"&season={r.choice(SEASONS)}", f"&collection={r.choice(COLLECTIONS)}", f"&format={r.choice(['TEST', 'ODI', 'T20'])}"])
    return "heavy", f"leaderboard_{kind}", f"/v1/leaderboards/{kind}?metric={metric}&limit={r.randint(10, 100)}{extra}"


def pick(r, mix):
    """(class, path): the class is the route's own, so the results can be read per class."""
    total = sum(w for _, w in MIXES[mix])
    x = r.uniform(0, total)
    for fn, w in MIXES[mix]:
        x -= w
        if x <= 0:
            return fn.__name__, fn(r)
    return MIXES[mix][-1][0].__name__, MIXES[mix][-1][0](r)


def classify(status):
    if status == 200:
        return "200"
    if status in (429, 503):
        return str(status)
    return "other_5xx" if isinstance(status, int) and status >= 500 else ("other_4xx" if isinstance(status, int) and status >= 400 else str(status))


def client_address(phase_id: int, index: int) -> str:
    """A valid, bounded IPv4 identity for caller `index` in phase `phase_id`, for any phase number. It used to be 10.PHASE.x.y, which is not an address once the phase number passes 255 (phase 256 starts after
    42.5 hours of ten-minute phases), and the API answered those requests with a 400. The second octet now wraps over 1 to 200, so every phase of a run of any length has valid, distinct callers."""
    if index < 0 or index >= 250 * 200:
        raise ValueError(f"caller {index} is outside the supported 0 to {250 * 200 - 1}")
    return f"10.{1 + (phase_id % 200)}.{index // 250}.{index % 250 + 1}"


live: list = []                  # the per-caller tallies of the phase in progress, for the partial reports


def client(phase, index, deadline, out, lock):
    r = random.Random(f"{phase['name']}-{index}")
    who = client_address(phase["id"], index)
    seen = {"count": {}, "lat200": [], "refusal_without_retry_after": 0, "n": 0, "cls": {}, "cls_lat": {}, "fam": {}, "lat_other": []}
    with lock:
        live.append(seen)                       # visible to the partial reports from the start
    while time.time() < deadline:
        if phase["mix"] == "final":
            cls, family, path = pick_final(r)
        else:
            (cls, path), family = pick(r, phase["mix"]), None
        t = time.perf_counter()
        retry = None
        try:
            c = Conn(60)
            c.request("GET", path, headers={"Host": "load.invalid", "X-Client-IP": who})
            resp = c.getresponse()
            resp.read()
            status = resp.status
            retry = resp.getheader("Retry-After")
            c.close()
        except socket.timeout:
            status = "timeout"
        except Exception as error:
            status = "error:" + type(error).__name__
        ms = (time.perf_counter() - t) * 1000
        name = classify(status)
        seen["count"][name] = seen["count"].get(name, 0) + 1
        seen["n"] += 1
        seen["cls"].setdefault(cls, {})[name] = seen["cls"].setdefault(cls, {}).get(name, 0) + 1
        if family:
            f = seen["fam"].setdefault(family, {"outcomes": {}, "lat": []})
            f["outcomes"][name] = f["outcomes"].get(name, 0) + 1
            if status == 200:
                f["lat"].append(ms)
        if status == 200:
            seen["lat200"].append(ms)
            seen["cls_lat"].setdefault(cls, []).append(ms)
        else:
            seen["lat_other"].append(ms)
        if status in (429, 503) and retry is None:
            seen["refusal_without_retry_after"] += 1
        if status in (429, 503) and phase["polite"]:
            time.sleep(min(5.0, float(retry) if retry and retry.replace(".", "", 1).isdigit() else 1.0) * (0.75 + r.random() * 0.5))
        elif phase.get("think_ms"):
            time.sleep(phase["think_ms"] / 1000 * (0.5 + r.random()))
    with lock:
        out.append(seen)
        live.remove(seen)


def probe(deadline, rows):
    while time.time() < deadline:
        t = time.perf_counter()
        try:
            c = Conn(5)
            c.request("GET", "/health", headers={"Host": "probe.invalid", "X-Client-IP": "10.250.0.1"})
            resp = c.getresponse()
            resp.read()
            status = resp.status
            c.close()
        except Exception as error:
            status = type(error).__name__
        rows.append((status, (time.perf_counter() - t) * 1000))
        time.sleep(0.25)


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


def summarize(phase, seen_list, took, rows, partial=False):
    """The record of a phase from the callers' tallies. With partial=True it is what had been seen when the phase was cut short, so an interrupted phase still leaves evidence and is marked as such."""
    counts, lat, other = {}, [], []
    for s in seen_list:
        lat += s["lat200"]
        other += s["lat_other"]
        for k, v in s["count"].items():
            counts[k] = counts.get(k, 0) + v
    total = sum(counts.values())
    by_class, class_lat, fams = {}, {}, {}
    for s in seen_list:
        for cls, c in s["cls"].items():
            for k, v in c.items():
                by_class.setdefault(cls, {})[k] = by_class.setdefault(cls, {}).get(k, 0) + v
        for cls, values in s["cls_lat"].items():
            class_lat.setdefault(cls, []).extend(values)
        for fam, f in s["fam"].items():
            g = fams.setdefault(fam, {"outcomes": {}, "lat": []})
            for k, v in f["outcomes"].items():
                g["outcomes"][k] = g["outcomes"].get(k, 0) + v
            g["lat"] += f["lat"]
    per_class = {cls: {"outcomes": c, "success_share": round(c.get("200", 0) / sum(c.values()), 4), "p50": pct(class_lat.get(cls, []), 0.5), "p99": pct(class_lat.get(cls, []), 0.99)} for cls, c in by_class.items()}
    per_family = {fam: {"requests": sum(g["outcomes"].values()), "outcomes": g["outcomes"], "p50": pct(g["lat"], 0.5), "p99": pct(g["lat"], 0.99)} for fam, g in sorted(fams.items())}
    health = [ms for st, ms in list(rows) if st == 200]
    record = {"phase": phase["name"], "clients": phase["clients"], "polite": phase["polite"], "seconds": round(took, 1), "requests": total, "per_s": round(total / took, 1) if took else 0,
              "outcomes": counts, "by_class": per_class, "by_family": per_family, "success_share": round(counts.get("200", 0) / total, 4) if total else None,
              "ok_latency_ms": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95), "p99": pct(lat, 0.99), "max": round(max(lat), 1) if lat else None},
              "non_200_latency_ms": {"n": len(other), "p50": pct(other, 0.5), "p99": pct(other, 0.99)},
              "refusals_without_retry_after": sum(s["refusal_without_retry_after"] for s in seen_list),
              "health": {"n": len(rows), "ok": len(health), "p50": pct(health, 0.5), "p99": pct(health, 0.99), "max": round(max(health), 1) if health else None,
                         "not_200": sorted({str(st) for st, _ in list(rows) if st != 200})}}
    # A 400 or 403 is not load behaviour: with a valid identity the generator never gets one, so any is evidence the run measured something else (an address the API refused, a malformed path).
    bad = sum(v for k, v in counts.items() if k in ("400", "403", "404"))
    record["invalid_evidence"] = bad > 0
    record["invalid_requests"] = bad
    if partial:
        record["partial"] = True
    return record


def run(phase):
    deadline = time.time() + phase["seconds"]
    out, lock, rows = [], threading.Lock(), []
    live.clear()
    threads = [threading.Thread(target=client, args=(phase, i, deadline, out, lock), daemon=True) for i in range(phase["clients"])]
    prober = threading.Thread(target=probe, args=(deadline + 0.5, rows), daemon=True)
    started = time.time()
    prober.start()
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
        prober.join()
    except BaseException:
        with lock:
            print(json.dumps(summarize(phase, out + list(live), time.time() - started, rows, partial=True)), flush=True)
        raise
    return summarize(phase, out, time.time() - started, rows)


def _on_term(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_term)
    for number, phase in enumerate(json.loads(sys.argv[1]), 1):
        phase["id"] = number
        print(json.dumps(run(phase)), flush=True)
