#!/usr/bin/env python3
"""A short same-client burst check of the token costs (audit B1), against the API's unix socket, standard library only.

    python burst_check.py PLAYER_ID [--rate 60] [--burst 20]

One client address, never used before (chosen at random from 10.240.0.0/12 on every run, so its bucket is full and no earlier run or load test has touched it), sends requests back to back. The limiter is a
token bucket: it holds up to `burst` tokens, gains rate/60 tokens a second, and takes the route's cost (1 for a player's batting or fielding career, 2 for the match history, 5 for a leaderboard) from a request it allows. A request that is not
allowed is answered 429 with Retry-After and takes nothing. Because the bucket refills while the requests are being sent, the check does not expect a fixed count: for each request it computes the tokens
the bucket should hold at that moment, from the time the request was sent, and then
  * a request sent when the bucket holds at least cost + 1 tokens must be answered 200
  * a request sent when it holds less than cost - 1 must be answered 429 with a Retry-After
  * in between (a token either way, for the time the request spent in flight) either answer is accepted, and the modelled bucket follows what the server actually did.
The first 429 must also arrive within the cost-based bound, and a run that never sees a 429 at all fails: the limit was not reached. Exit status 1 on any failure.
"""
import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import socket_load as sl  # noqa: E402

TOLERANCE = 1.0


def fresh_address(rng=None) -> str:
    rng = rng or random.SystemRandom()
    return f"10.{240 + rng.randrange(16)}.{rng.randrange(256)}.{rng.randrange(1, 255)}"


def judge(events: list, cost: int, rate_per_minute: float, burst: float) -> tuple:
    """events: (sent_at_seconds, status, retry_after). Returns (problems, first_refusal_index or None). The bucket model starts full at the first request."""
    problems, tokens, last, refused_at = [], float(burst), None, None
    for i, (t, status, retry) in enumerate(events):
        if last is not None:
            tokens = min(float(burst), tokens + (t - last) * rate_per_minute / 60.0)
        last = t
        if tokens >= cost + TOLERANCE and status != 200:
            problems.append(f"request {i + 1} was answered {status} with about {tokens:.1f} tokens in the bucket (cost {cost})")
        if tokens < cost - TOLERANCE and status != 429:
            problems.append(f"request {i + 1} was answered {status} with only about {tokens:.1f} tokens in the bucket (cost {cost})")
        if status == 429:
            refused_at = i if refused_at is None else refused_at
            if not retry:
                problems.append(f"request {i + 1}: the 429 has no Retry-After")
        elif status == 200:
            tokens -= cost
    if refused_at is None:
        problems.append(f"no request was refused in {len(events)}: the limit was not reached")
    return problems, refused_at


def burst(address: str, path: str, count: int, sock_path=None) -> list:
    out = []
    start = time.monotonic()
    for _ in range(count):
        sent = time.monotonic() - start
        c = sl.Conn(10)
        c.request("GET", path, headers={"Host": "burst.invalid", "X-Client-IP": address})
        r = c.getresponse()
        r.read()
        out.append((sent, r.status, r.getheader("Retry-After")))
        c.close()
    return out


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("player")
    ap.add_argument("--rate", type=float, default=60)
    ap.add_argument("--burst", type=float, default=20)
    a = ap.parse_args(argv)
    problems = []
    # The costs after the correction batch of 2026-10-14: a batting career and a fielding career 1, the match history 2, a leaderboard 5.
    for label, path, cost, count in (("career", f"/v1/players/{a.player}/batting", 1, 40), ("fielding", f"/v1/players/{a.player}/fielding", 1, 40), ("history", f"/v1/players/{a.player}/matches?limit=5", 2, 24),
                                     ("leaderboard", "/v1/leaderboards/batting?metric=runs&limit=10", 5, 12)):
        address = fresh_address()
        events = burst(address, path, count, None)
        found, first = judge(events, cost, a.rate, a.burst)
        print(f"{label} (cost {cost}, caller {address}): {[e[1] for e in events]}, first refusal at request {None if first is None else first + 1}")
        problems += [f"{label}: {p}" for p in found]
    print("burst check:", "FAILED" if problems else "ok", *problems, sep="\n  " if problems else " ")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
