#!/usr/bin/env python3
"""Record the answers of a fixed set of read-only requests from one version of the code, and replay them against another (or against the live API over its socket), comparing every body.

    record_replay.py urls   --data DATA_DIR --out URLS.json                      the request set, drawn from the data with a fixed seed
    record_replay.py record --tree REPO --data DATA_DIR --urls URLS.json --out RECORDED.json
                                                                                  run the API of that source tree in-process (needs its dependencies) and keep status and body of each request
    record_replay.py compare A.json B.json                                       exit 1 if any status or body differs (the request id is ignored)
    record_replay.py replay --recorded RECORDED.json [--socket PATH]             send the recorded requests to the live API's socket (standard library only, run as root on the server), compare

The set: every leaderboard metric under many filters and thresholds; for 67 players (20 career-size bands of three, the busiest, the ladder's player and players with no career) the batting and bowling
career under five filter sets, seasons under two, splits by four dimensions, fielding career, and the match history under two filter sets; 30 scorecards across the formats; and a few batter and bowler pairs.
Each request has its own client address, so the rate limit never answers instead of the route. Read-only: nothing is written anywhere.
"""
import argparse
import http.client
import json
import os
import random
import re
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

RID = re.compile(r'"request_id":"[0-9a-f]+"')
# A cursor is signed with the data version, so it differs between two versions of the data by design. Whether there is a next page must still match; the value is not compared.
CURSOR = re.compile(r'"next_cursor":"[^"]+"')


def build_urls(data: Path) -> list:
    import duckdb
    meta = json.loads((data / "meta.json").read_text())
    con = duckdb.connect(":memory:")
    for name, t in meta["tables"].items():
        con.execute(f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet(\'{data / t["file"]}\')')
    rnd = random.Random(11)
    urls = []
    BAT = ["runs", "average", "strike_rate", "hundreds", "fifties", "fours", "sixes", "highest_score"]
    BOWL = ["wickets", "maidens", "five_wicket_hauls", "average", "economy", "strike_rate", "best_figures"]
    colls = [r[0] for r in con.execute("SELECT DISTINCT collection FROM matches WHERE collection IS NOT NULL ORDER BY 1").fetchall()]
    seasons = [r[0] for r in con.execute("SELECT DISTINCT season FROM matches WHERE season IS NOT NULL ORDER BY 1").fetchall()]
    dates = [str(r[0]) for r in con.execute("SELECT DISTINCT start_date FROM matches ORDER BY 1").fetchall()]
    filters = [{}] + [{"collection": c} for c in colls] + [{"format": f} for f in ("TEST", "ODI", "T20")] + [{"gender": g} for g in ("male", "female")] + [{"season": s} for s in rnd.sample(seasons, 8)]
    filters += [{"date_from": a, "date_to": b} for a, b in (sorted(rnd.sample(dates, 2)) for _ in range(6))] + [{"collection": "tests", "format": "ODI"}, {"date_from": "1990-01-01", "date_to": "1990-12-31"}]
    for kind, metrics in (("batting", BAT), ("bowling", BOWL)):
        for m in metrics:
            urls += [(f"/v1/leaderboards/{kind}", {"metric": m, **f}) for f in filters]
            urls += [(f"/v1/leaderboards/{kind}", {"metric": m, **e}) for e in ({"limit": 1}, {"limit": 100}, {"limit": 7, "min_balls": 1}, {"limit": 50, "min_balls": 100000}, {"limit": 20, "collection": "ipl", "min_balls": 1})]
    ranked = [r[0] for r in con.execute("""SELECT player_id FROM (SELECT player_id, sum(b) t FROM (SELECT player_id, sum(balls) b FROM player_batting_innings GROUP BY 1
                                          UNION ALL SELECT player_id, sum(balls) FROM player_bowling_spells GROUP BY 1) GROUP BY 1) ORDER BY t DESC, 1""").fetchall()]
    players = ranked[:3] + ["e087956b"]
    for k in range(20):
        players += rnd.sample(ranked[k * len(ranked) // 20:(k + 1) * len(ranked) // 20], 3)
    players += [r[0] for r in con.execute("SELECT player_id FROM players WHERE player_id NOT IN (SELECT player_id FROM player_batting_innings UNION SELECT player_id FROM player_bowling_spells) ORDER BY 1 LIMIT 3").fetchall()]
    players = list(dict.fromkeys(players))
    five = [{}, {"format": "T20"}, {"collection": "ipl"}, {"gender": "female"}, {"date_from": "2015-01-01", "date_to": "2019-12-31"}]
    for pid in players:
        for kind in ("batting", "bowling", "fielding"):
            urls += [(f"/v1/players/{pid}/{kind}", f) for f in five[:2 if kind == "fielding" else 5]]
            urls += [(f"/v1/players/{pid}/{kind}/seasons", f) for f in ({}, {"format": "ODI"})]
            urls += [(f"/v1/players/{pid}/{kind}/splits", {"by": by}) for by in ("opponent", "venue", "competition", "format")] + [(f"/v1/players/{pid}/{kind}/splits", {"by": "opponent", "format": "T20", "limit": 5})]
        urls += [(f"/v1/players/{pid}/matches", {"limit": 50}), (f"/v1/players/{pid}/matches", {"limit": 30, "format": "T20"})]
    for fmt in ("TEST", "ODI", "T20"):
        rows = [r[0] for r in con.execute(f"SELECT match_id FROM matches WHERE format = '{fmt}' ORDER BY match_id").fetchall()]
        urls += [(f"/v1/matches/{m}/scorecard", {}) for m in rnd.sample(rows, 10)]
    pairs = con.execute("SELECT striker_id, bowler_id FROM balls WHERE striker_id IS NOT NULL AND bowler_id IS NOT NULL GROUP BY 1, 2 HAVING count(*) > 30 ORDER BY hash(striker_id || bowler_id) LIMIT 6").fetchall()
    urls += [("/v1/matchups", {"batter": b, "bowler": w}) for b, w in pairs]
    return [[p, q] for p, q in urls]


def qs(q: dict) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in q.items())


def record(tree: Path, data: Path, urls: list) -> dict:
    sys.path.insert(0, str(tree / "api"))
    os.environ.update({"CRICKET_API_DATA": str(data), "CRICKET_API_RATE": "10000000", "CRICKET_API_BURST": "10000000", "CRICKET_API_QUERY_SECONDS": "30", "CRICKET_API_REQUEST_SECONDS": "60", "CRICKET_API_SECRET": "r" * 40})
    from fastapi.testclient import TestClient
    from cricket_api.app import create_app
    client = TestClient(create_app())
    out = {}
    for n, (path, q) in enumerate(urls):
        r = client.get(path, params=q, headers={"X-Client-IP": f"10.77.{n // 250}.{n % 250 + 1}"})
        out[path + "?" + qs(q)] = {"status": r.status_code, "body": r.text}
    return out


def same(a: dict, b: dict) -> bool:
    def clean(body: str) -> str:
        return CURSOR.sub('"next_cursor":"<cursor>"', RID.sub("", body))
    return a["status"] == b["status"] and clean(a["body"]) == clean(b["body"])


def compare(x: dict, y: dict) -> list:
    return [k for k in x if k not in y or not same(x[k], y[k])] + [k for k in y if k not in x]


class UnixConn(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=60); self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.sock.settimeout(60); self.sock.connect(self.path)


def replay(recorded: dict, sock: str) -> tuple:
    conn, bad, codes, t0 = UnixConn(sock), [], {}, time.time()
    for n, (key, rec) in enumerate(recorded.items()):
        conn.request("GET", key, headers={"Host": "replay.invalid", "X-Client-IP": f"10.88.{n // 250}.{n % 250 + 1}"})
        r = conn.getresponse(); body = r.read().decode(); codes[r.status] = codes.get(r.status, 0) + 1
        if not same({"status": r.status, "body": body}, rec):
            bad.append(key)
    return bad, codes, time.time() - t0


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("urls"); u.add_argument("--data", required=True); u.add_argument("--out", required=True)
    r = sub.add_parser("record"); r.add_argument("--tree", required=True); r.add_argument("--data", required=True); r.add_argument("--urls", required=True); r.add_argument("--out", required=True)
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    p = sub.add_parser("replay"); p.add_argument("--recorded", required=True); p.add_argument("--socket", default="/srv/cricket/run/api.sock")
    a = ap.parse_args(argv)
    if a.cmd == "urls":
        urls = build_urls(Path(a.data)); Path(a.out).write_text(json.dumps(urls)); print(f"{len(urls)} requests"); return 0
    if a.cmd == "record":
        out = record(Path(a.tree), Path(a.data), json.loads(Path(a.urls).read_text())); Path(a.out).write_text(json.dumps(out))
        print(f"{len(out)} responses, statuses {sorted({v['status'] for v in out.values()})}"); return 0
    if a.cmd == "compare":
        x, y = json.loads(Path(a.a).read_text()), json.loads(Path(a.b).read_text()); bad = compare(x, y)
        print(f"{len(x)} requests compared, {len(bad)} differ; statuses {sorted({v['status'] for v in x.values()})}")
        for k in bad[:10]: print("DIFF", k)
        return 1 if bad else 0
    rec = json.loads(Path(a.recorded).read_text()); bad, codes, took = replay(rec, a.socket)
    print(f"{len(rec)} requests in {took:.0f} s; differ {len(bad)}; status codes {codes}")
    for k in bad[:10]: print("DIFF", k)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
