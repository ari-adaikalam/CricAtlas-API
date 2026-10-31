"""The rate limiter's memory is bounded, and bounding it never frees a client that still owes tokens.

A bucket that has refilled is the same as no bucket, so it is dropped. When the table is full of clients that still owe tokens, clients the table has not seen share one overflow bucket,
and nobody already in the table is reset. The decisions must be the same as a limiter with no bound at all, which a plain reference checks over random traffic.
"""
import random

import pytest

from cricket_api.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Reference:
    """The token bucket written the plain way: one bucket per client, never dropped."""
    def __init__(self, per_minute, burst):
        self.rate, self.capacity, self.buckets = per_minute / 60.0, float(max(burst, 1)), {}

    def take(self, client, cost, now):
        tokens, stamp = self.buckets.get(client, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - stamp) * self.rate)
        allowed = tokens >= cost
        if allowed:
            tokens -= cost
        self.buckets[client] = (tokens, now)
        return allowed, int(tokens)


@pytest.mark.parametrize("seed", range(6))
def test_dropping_refilled_buckets_never_changes_a_decision(seed):
    """Over random traffic from a small set of clients, with a long gap now and then, the limiter agrees with the reference on every answer and on the remaining tokens."""
    rng, clock = random.Random(seed), Clock()
    limiter, reference = RateLimiter(per_minute=60, burst=8, clock=clock), Reference(60, 8)
    clients = [f"c{i}" for i in range(12)]
    dropped = 0
    for step in range(4000):
        clock.now += rng.choice([0.0, 0.25, 0.25, 0.5, 1.0, 3.0, 40.0])      # exact in binary, so the two compute the same numbers
        client, cost = rng.choice(clients), rng.choice([1, 1, 1, 3, 5])
        before = limiter.clients()
        got = limiter.take(client, cost)
        want, remaining = reference.take(client, cost, clock.now)
        assert (got.allowed, got.remaining) == (want, remaining), (step, client, cost)
        dropped += before > limiter.clients()
    assert dropped > 0, "the run did drop refilled buckets, so the check means something"
    assert limiter.clients() <= len(clients)


def test_a_full_bucket_is_not_kept_and_a_spent_one_is_kept_until_it_refills():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=4, clock=clock)
    limiter.take("a", 3)                                      # one token left, full again in 3 s
    assert limiter.clients() == 1
    clock.now += 2.9
    limiter.take("b", 1)
    assert limiter.clients() == 2, "a is a fraction of a token short of full"
    clock.now += 0.2
    limiter.take("c", 1)
    assert limiter.clients() == 2, "a refilled and was dropped"
    assert limiter.take("a", 4).allowed, "and a dropped client starts again with a full bucket, which is what it had"


def test_a_cost_above_the_burst_never_stores_anything():
    limiter = RateLimiter(per_minute=60, burst=3, clock=Clock())
    assert not limiter.take("a", 5).allowed and limiter.clients() == 0


def test_a_flood_of_new_keys_never_resets_a_client_that_is_out_of_tokens():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=5, max_clients=100, clock=clock)
    assert [limiter.take("victim").allowed for _ in range(5)] == [True] * 5 and not limiter.take("victim").allowed
    for i in range(20_000):                                   # 200 times the table
        limiter.take(f"flood{i}")
    assert limiter.clients() <= 100
    assert not limiter.take("victim").allowed, "the exhausted client is still refused after the flood"
    clock.now += 2.0
    assert limiter.take("victim").allowed, "and it earns its tokens back at the normal rate"


def test_when_the_table_is_full_unseen_clients_share_one_allowance_and_tracked_clients_keep_theirs():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=3, max_clients=4, clock=clock)
    for name in "abcd":
        limiter.take(name)                                    # four clients that owe tokens fill the table
    assert limiter.clients() == 4
    newcomers = [limiter.take(f"new{i}").allowed for i in range(8)]
    assert newcomers == [True] * 3 + [False] * 5, "the unseen share the allowance of one client"
    assert limiter.clients() == 4 and limiter.overflowed == 8
    assert limiter.take("a").allowed and limiter.take("a").allowed and not limiter.take("a").allowed, "a tracked client still has its own bucket"
    clock.now += 4.0                                          # everyone has refilled, so the table empties and newcomers get their own buckets again
    assert limiter.take("new0").allowed and limiter.take("new1").allowed and limiter.take("new0").allowed
    assert limiter.clients() == 2 and limiter.overflowed == 8


def test_the_overflow_bucket_refills_like_any_other():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=2, max_clients=1, clock=clock)
    limiter.take("a", 2)                                      # a is empty and stays in the table for 2 s
    assert [limiter.take(f"x{i}").allowed for i in range(4)] == [True, True, False, False]
    clock.now += 1.0
    assert limiter.take("x9").allowed and not limiter.take("x10").allowed


def test_the_refusal_to_an_unseen_client_carries_a_retry_after():
    clock = Clock()
    limiter = RateLimiter(per_minute=60, burst=1, max_clients=1, clock=clock)
    limiter.take("a")
    limiter.take("x0")
    refused = limiter.take("x1")
    assert not refused.allowed and refused.retry_after >= 1 and refused.limit == 60


def test_the_internal_queue_stays_small_when_one_client_sends_many_requests():
    clock = Clock()
    limiter = RateLimiter(per_minute=6000, burst=10, clock=clock)
    for _ in range(20_000):
        limiter.take("busy")
        clock.now += 0.001
    assert limiter.clients() == 1 and len(limiter._due) <= 4 * limiter.clients() + 64 + 1


def test_a_limiter_that_never_refills_keeps_its_buckets_and_stays_bounded():
    limiter = RateLimiter(per_minute=0, burst=2, max_clients=3, clock=Clock())
    for i in range(10):
        limiter.take(f"k{i}")
    assert limiter.clients() == 3 and limiter._due == []


def test_the_table_never_holds_more_than_its_bound_under_random_traffic():
    rng, clock = random.Random(5), Clock()
    limiter = RateLimiter(per_minute=60, burst=5, max_clients=50, clock=clock)
    for _ in range(30_000):
        clock.now += rng.choice([0.0, 0.01, 0.5])
        limiter.take(f"k{rng.randrange(400)}", rng.choice([1, 3]))
        assert limiter.clients() <= 50
